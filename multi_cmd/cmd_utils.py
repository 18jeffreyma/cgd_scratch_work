import torch
torch.backends.cudnn.benchmark = True

import torch.autograd as autograd

from multi_cmd import potentials

import time

def zero_grad(params):
    """Given some list of Tensors, zero and reset gradients."""
    for p in params:
        if p.grad is not None:
            p.grad.detach()
            p.grad.zero_()


def flatten_filter_none(grad_list, param_list, detach=False, neg=False):
    """
    Given a list of Tensors with possible None values, returns single Tensor
    with None removed and flattened.
    """
    filtered = []
    for grad, param in zip(grad_list, param_list):
        if grad is None:
            filtered.append(torch.zeros(param.numel(), requires_grad=True))
        else:
            filtered.append(grad.contiguous().view(-1))

    result = torch.cat(filtered) if not neg else -torch.cat(filtered)

    # Use this only if higher order derivatives are not needed.
    if detach:
        result.detach_()

    return result


def avp(
    hessian_loss_list,
    player_list,
    player_list_flattened,
    vector_list_flattened,
    bregman=potentials.squared_distance(1),
    transpose=False,
):
    """
    :param hessian_loss_list: list of objective functions for hessian computation
    :param player_list: list of list of params for each player to compute gradients from
    :param player_list_flattened: list of flattened player tensors (without gradients)
    :param vector_list_flattened: list of flattened vectors for each player
    :param bregman: dictionary representing bregman potential to use
    :param transpose: compute product against transpose if set

    Computes right product of metamatrix with a vector of player vectors.
    """

    # TODO(jjma): add error handling and assertions
    # assert(len(hessian_loss_list) == len(player_list))
    # assert(len(hessian_loss_list) == len(vector_list))
    prod_list = [torch.zeros_like(v) for v in vector_list_flattened]

    for i, row_params in enumerate(player_list):
        for j, (col_params, vector_elem) in enumerate(zip(player_list, vector_list_flattened)):

            if i == j:
                # Diagonal element is the Bregman term.

                # TODO(jjma): Check if all Bregman potentials can be evaluated
                # element-wise; if so, we can evaluate this tensor by tensor as
                # below.
                prod_list[i] += bregman['Dxx_vp'](player_list_flattened[i], vector_elem)
                continue

            # Otherwise, we construct our Hessian vector products. Variable
            # retain_graph must be set to true, or we cant compute multiple
            # subsequent Hessians any more.

            # TODO(jjma): Hessian vector product calculation is our biggest
            # bottlenecking step, which makes metamatrix vector product inefficient.
            loss = hessian_loss_list[i] if not transpose else hessian_loss_list[j]

            # start = time.time()
            grad_raw = autograd.grad(loss, col_params,
                                     create_graph=True,
                                     retain_graph=True,
                                     allow_unused=True)
            grad_flattened = flatten_filter_none(grad_raw, col_params)
            # print('grad_time', time.time() - start)

            # start = time.time()
            # Don't need any higher order derivatives, so create_graph = False.
            hvp_raw = autograd.grad(grad_flattened, row_params,
                                    grad_outputs=vector_elem,
                                    create_graph=False,
                                    retain_graph=True,
                                    allow_unused=True)
            hvp_flattened = flatten_filter_none(hvp_raw, row_params)
            # print('hessian_time', time.time() - start)

            prod_list[i] += hvp_flattened

    return prod_list


def atvp(
    hessian_loss_list,
    player_list,
    player_list_flattened,
    vector_list_flattened,
    bregman=potentials.squared_distance(1),
):
    """
    :param hessian_loss_list: list of objective functions for hessian computation
    :param player_list: list of list of params for each player to compute gradients from
    :param player_list_flattened: list of flattened player tensors (without gradients)
    :param vector_list_flattened: list of flattened vectors for each player
    :param bregman: dictionary representing bregman potential to use

    Computes right product of transposed metamatrix with a vector of player vectors.
    """
    # TODO(jjma): add error handling and assertions
    # assert(len(hessian_loss_list) == len(player_list))
    # assert(len(hessian_loss_list) == len(vector_list))
    prod_list = [torch.zeros_like(v) for v in vector_list_flattened]

    # Since we have overlap at first derivative, pre-compute gradient tensors.
    col_grads_flattened = []
    for j, col_params in enumerate(player_list):
        grad_raw = autograd.grad(hessian_loss_list[j], col_params,
                                 create_graph=True,
                                 retain_graph=True,
                                 allow_unused=True)
        grad_flattened = flatten_filter_none(grad_raw, col_params)
        col_grads_flattened.append(grad_flattened)

    for i, row_params in enumerate(player_list):
        for j, vector_elem in enumerate(vector_list_flattened):
            if i == j:
                # Diagonal element is the Bregman term.
                prod_list[i] += bregman['Dxx_vp'](player_list_flattened[i], vector_elem)
                continue

            # Don't need any higher order derivatives, so create_graph = False.
            hvp_raw = autograd.grad(col_grads_flattened[j], row_params,
                                    grad_outputs=vector_elem,
                                    create_graph=False,
                                    retain_graph=True,
                                    allow_unused=True)
            hvp_flattened = flatten_filter_none(hvp_raw, row_params)

            prod_list[i] += hvp_flattened

    return prod_list


def metamatrix_conjugate_gradient(
    grad_loss_list,
    hessian_loss_list,
    player_list,
    player_list_flattened,
    vector_list_flattened=None,
    bregman=potentials.squared_distance(1),
    n_steps=5,
    tol=1e-6,
    atol=1e-6,
):
    """
    :param grad_loss_list: list of loss tensors for each player to compute gradient
    :param hessian_loss_list: list of loss tensors for each player to compute hessian
    :param player_list: list of list of params for each player to compute gradients from
    :param player_list_flattened: list of flattened player tensors (without gradients)
    :param vector_list_flattened: initial guess for update solution
    :param bregman: dict representing a Bregman potential to be used
    :param n_steps: number of iteration steps for conjugate gradient
    :param tol: relative residual tolerance threshold from initial vector guess
    :param atol: absolute residual tolerance threshold

    Compute solution to meta-matrix game form using conjugate gradient method. Since
    the metamatrix A is not p.s.d, we multiply both sides by the transpose to
    ensure p.s.d.

    In other words, note that solving Ax = b (where A is meta matrix, x is
    vector of update vectors and b is learning rate times vector of gradients
    is the same as solving A'x = b' (where A' = (A^T)A and b' = (A^T)b.
    """

    b = []
    for loss, param_tensors in zip(grad_loss_list, player_list):
        # Get vector list of negative gradients.
        grad_raw = autograd.grad(loss, param_tensors,
                                 retain_graph=True,
                                 allow_unused=True)
        grad_flattened = flatten_filter_none(grad_raw, param_tensors,
                                             neg=True, detach=True)
        b.append(grad_flattened)

    # Multiplying both sides by transpose to ensure p.s.d.
    # r = A^t * b (before we subtract)
    # TODO(jjma): This single metamatrix product takes 0.4s.
    # r = atvp(hessian_loss_list, player_list, player_list_flattened, b,
    #          bregman=bregman)

    r = avp(hessian_loss_list, player_list, player_list_flattened, b,
            bregman=bregman, transpose=True)

    # Set relative residual threshold based on norm of b.
    norm_At_b = sum(torch.dot(r_elem, r_elem) for r_elem in r)
    residual_tol = tol * norm_At_b

    # If no guess provided, start from zero vector.
    if vector_list_flattened is None:
        vector_list_flattened = [torch.zeros_like(p) for p in player_list_flattened]
    else:
        # Compute initial residual if a guess is given.
        A_x = avp(hessian_loss_list, player_list, player_list_flattened, vector_list_flattened,
                  bregman=bregman, transpose=False)
        # At_A_x = atvp(hessian_loss_list, player_list, player_list_flattened, A_x,
        #               bregman=bregman)

        At_A_x = avp(hessian_loss_list, player_list, player_list_flattened, A_x,
                     bregman=bregman, transpose=True)

        torch._foreach_sub_(r, At_A_x)


    # Early exit if solution already found.
    rdotr = sum(torch.dot(r_elem, r_elem) for r_elem in r)
    if rdotr < residual_tol or rdotr < atol:
        return vector_list_flattened, 0

    # Define p and measure current candidate vector.
    p = [r_elem.clone().detach() for r_elem in r]

    # Use conjugate gradient to find vector solution.
    for i in range(n_steps):
        A_p = avp(hessian_loss_list, player_list, player_list_flattened, p,
                  bregman=bregman, transpose=False)
        # At_A_p = atvp(hessian_loss_list, player_list, player_list_flattened, A_p,
        #               bregman=bregman)

        At_A_p = avp(hessian_loss_list, player_list, player_list_flattened, A_p,
                     bregman=bregman, transpose=True)

        with torch.no_grad():
            alpha = torch.div(rdotr, sum(torch.dot(e1, e2) for e1, e2 in zip(p, At_A_p)))

            # Update candidate solution and residual, where:
            # (1) x_new = x + alpha * p
            # (2) r_new = r - alpha * A' * p
            torch._foreach_add_(vector_list_flattened, p, alpha=alpha)
            torch._foreach_sub_(r, At_A_p, alpha=alpha)

            # Calculate new residual metric
            new_rdotr = sum(torch.dot(r_elem, r_elem) for r_elem in r)

            # Break if solution is within threshold
            if new_rdotr < atol or new_rdotr < residual_tol:
                break

            # Otherwise, update and continue.
            # (3) p_new = r_new + beta * p
            beta = torch.div(new_rdotr, rdotr)
            p = torch._foreach_add(r, p, alpha=beta)

            rdotr = new_rdotr

    return vector_list_flattened, i+1


def exp_map(player_list_flattened, nash_list_flattened,
            bregman=potentials.squared_distance(1),
            in_place=True
):
    """
    :param player_list: list of player params before update
    :param nash_list: nash equilibrium solutions computed from minimization step

    Map dual system coordinate solution back to primal, accounting
    for feasibility constraints specified in Bregman potential.
    """
    with torch.no_grad():
        mapped = [bregman['Dx'](bregman['Dx_inv'](param) + bregman['Dxx_vp'](param, nash))
                  for param, nash in zip(player_list_flattened, nash_list_flattened)]

    return mapped


# TODO(jjma): make this user interface cleaner.
class CMD(object):
    """Optimizer class for the CMD algorithm with differentiable player objectives."""
    def __init__(self, player_list,
                 bregman=potentials.squared_distance(1),
                 tol=1e-6, atol=1e-6,
                 device=torch.device('cpu')
                ):
        """
        :param player_list: list (per player) of list of Tensors, representing parameters
        :param bregman: dict representing Bregman potential to be used
        """
        self.bregman = bregman

        # In case, parameter generators are provided.
        player_list = [list(elem) for elem in player_list]

        # Store optimizer state.
        self.state = {'step': 0,
                      'player_list': player_list,
                      'tol': tol, 'atol': atol,
                      'last_dual_soln': None,
                      'last_dual_soln_n_iter': 0}
        # TODO(jjma): set this device in CMD algorithm.
        self.device = device

    def zero_grad(self):
        for player in self.state['player_list']:
            zero_grad(player)

    def state_dict(self):
        return self.state

    def player_list(self):
        return self.state['player_list']

    def step(self, loss_list):
        # Compute flattened player list for some small optimization.
        player_list = self.state['player_list']
        player_list_flattened = [flatten_filter_none(player, player, detach=True)
                                 for player in player_list]

        # Compute dual solution first, before mapping back to primal.
        # Use dual solution as initial guess for numerical speed.
        nash_list_flattened, n_iter = metamatrix_conjugate_gradient(
            loss_list,
            loss_list,
            player_list,
            player_list_flattened,
            vector_list_flattened=self.state['last_dual_soln'],
            bregman=self.bregman,
            tol=self.state['tol'],
            atol=self.state['atol']
        )

        # Store state for use in next nash computation..
        self.state['step'] += 1
        self.state['last_dual_soln'] = nash_list_flattened
        self.state['last_dual_soln_n_iter'] = n_iter

        # Map dual solution back into primal space.
        mapped_list_flattened = exp_map(player_list_flattened,
                                        nash_list_flattened,
                                        bregman=self.bregman)

        # Update parameters in place to update players as optimizer.
        for player, mapped_flattened in zip(self.state['player_list'], mapped_list_flattened):
            idx = 0
            for p in player:
                p.data = mapped_flattened[idx: idx + p.numel()].reshape(p.shape)
                idx += p.numel()


class CMD_RL(CMD):
    """RL optimizer using CMD algorithm, using derivation from CoPG paper."""
    def __init__(self, player_list,
                 bregman=potentials.squared_distance(1),
                 tol=1e-6, atol=1e-6,
                 device=torch.device('cpu')
                ):
        """
        :param player_list: list (per player) of list of Tensors, representing parameters
        :param bregman: dict representing Bregman potential to be used
        """
        super(CMD_RL, self).__init__(player_list,
                                     bregman=bregman,
                                     tol=tol, atol=atol,
                                     device=device)

    def step(self, grad_loss_list, hessian_loss_list):
        """
        CMD algorithm using derivation for gradient and hessian term from CoPG.
        """
        # Compute flattened player list for some small optimization.
        player_list = self.state['player_list']
        player_list_flattened = [flatten_filter_none(player, player, detach=True)
                                 for player in player_list]

        # Compute dual solution first, before mapping back to primal.
        # Use dual solution as initial guess for numerical speed.
        nash_list_flattened, n_iter = metamatrix_conjugate_gradient(
            grad_loss_list,
            hessian_loss_list,
            player_list,
            player_list_flattened,
            vector_list_flattened=self.state['last_dual_soln'],
            bregman=self.bregman,
            tol=self.state['tol'],
            atol=self.state['atol']
        )

        # Store state for use in next nash computation..
        self.state['step'] += 1
        self.state['last_dual_soln'] = nash_list_flattened
        self.state['last_dual_soln_n_iter'] = n_iter

        # Map dual solution back into primal space.
        mapped_list_flattened = exp_map(player_list_flattened,
                                        nash_list_flattened,
                                        bregman=self.bregman)

        # Update parameters in place to update players as optimizer.
        for player, mapped_flattened in zip(self.state['player_list'], mapped_list_flattened):
            idx = 0
            for p in player:
                p.data = mapped_flattened[idx: idx + p.numel()].reshape(p.shape)
                idx += p.numel()
