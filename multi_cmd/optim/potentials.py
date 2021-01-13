"""Provided Bregman potentials."""
import torch

def squared_distance(alpha):
    """
    This potential recovers the CGD algorithm, with no value constraints
    """
    def Dx(x_dual):
        return x_dual / alpha

    def Dx_inv(x_dual):
        return x_dual * alpha

    def Dxx_vp(x_primal, vec):
        # Does not need to be in-place.
        return vec * alpha

    return {'Dx': Dx, 'Dx_inv': Dx_inv, 'Dxx_vp': Dxx_vp}


def shannon_entropy(alpha):
    """
    This potential recovers the CMW algorithm, constraining weights
    to positive values only.
    """
    def Dx(x_dual):
        return torch.exp(x_dual / alpha)

    def Dx_inv(x_primal):
        return alpha * torch.log(x_primal)

    def Dxx_vp(x_primal, vec):
        # Does not need to be in-place.
        return vec / x_primal * alpha

    return {'Dx': Dx, 'Dx_inv': Dx_inv, 'Dxx_vp': Dxx_vp}