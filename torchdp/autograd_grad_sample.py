#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
"""
Taken from https://github.com/cybertronai/autograd-hacks

Original license is Unlicense. We put it here for user's convenience, with
the author's permission.
"""

from typing import List

import torch
import torch.nn as nn
from torch.functional import F

from .utils import get_layer_type, requires_grad, sum_over_all_but_batch_and_last_n


_supported_layers = [
    "Linear",
    "Conv2d",
    "Conv1d",
    "LayerNorm",
    "GroupNorm",
    "InstanceNorm1d",
    "InstanceNorm2d",
    "InstanceNorm3d",
    "SequenceBias",
]  # Supported layer class types

# work-around for https://github.com/pytorch/pytorch/issues/25723
_hooks_disabled: bool = False

# global switch to catch double backprop errors on Hessian computation
_enforce_fresh_backprop: bool = False


def add_hooks(model: nn.Module) -> None:
    """
    Adds hooks to model to save activations and backprop values.
    The hooks will
    1. save activations into param.activations during forward pass
    2. append backprops to params.backprops_list during backward pass.
    Call "remove_hooks(model)" to disable this.
    Args:
        model:
    """

    global _hooks_disabled
    _hooks_disabled = False

    handles = []
    for layer in model.modules():
        if get_layer_type(layer) in _supported_layers:
            handles.append(layer.register_forward_hook(_capture_activations))
            handles.append(layer.register_backward_hook(_capture_backprops))

    model.__dict__.setdefault("autograd_grad_sample_hooks", []).extend(handles)


def remove_hooks(model: nn.Module) -> None:
    """
    Remove hooks added by add_hooks(model)
    """
    if not hasattr(model, "autograd_grad_sample_hooks"):
        raise ValueError("Asked to remove hooks, but no hooks found")
    else:
        for handle in model.autograd_grad_sample_hooks:
            handle.remove()
        del model.autograd_grad_sample_hooks


def disable_hooks() -> None:
    """
    Globally disable all hooks installed by this library.
    """

    global _hooks_disabled
    _hooks_disabled = True


def enable_hooks() -> None:
    """the opposite of disable_hooks()"""

    global _hooks_disabled
    _hooks_disabled = False


def is_supported(layer: nn.Module) -> bool:
    """Check if this layer is supported"""

    return get_layer_type(layer) in _supported_layers


def _capture_activations(
    layer: nn.Module, input: List[torch.Tensor], output: torch.Tensor
):
    """Save activations into layer.activations in forward pass"""

    if _hooks_disabled:
        return
    if get_layer_type(layer) not in _supported_layers:
        raise ValueError("Hook installed on unsupported layer")

    layer.activations = input[0].detach()


def _capture_backprops(layer: nn.Module, _input, output):
    """Append backprop to layer.backprops_list in backward pass."""
    global _enforce_fresh_backprop

    if _hooks_disabled:
        return

    if _enforce_fresh_backprop:
        if hasattr(layer, "backprops_list"):
            raise ValueError(
                f"Seeing result of previous backprop, "
                f"use clear_backprops(model) to clear"
            )
        _enforce_fresh_backprop = False

    if not hasattr(layer, "backprops_list"):
        layer.backprops_list = []
    layer.backprops_list.append(output[0].detach())


def clear_backprops(model: nn.Module) -> None:
    """Delete layer.backprops_list in every layer."""
    for layer in model.modules():
        if hasattr(layer, "backprops_list"):
            del layer.backprops_list


def compute_grad_sample(
    model: nn.Module, loss_type: str = "mean", batch_dim: int = 0
) -> None:
    """
    Compute per-example gradients and save them under 'param.grad_sample'.
    Must be called after loss.backprop()
    Args:
        model:
        loss_type: either "mean" or "sum" depending whether backpropped
        loss was averaged or summed over batch
    """

    if loss_type not in ("sum", "mean"):
        raise ValueError(f"loss_type = {loss_type}. Only 'sum' and 'mean' supported")
    for layer in model.modules():
        layer_type = get_layer_type(layer)
        if not requires_grad(layer) or layer_type not in _supported_layers:
            continue
        if not hasattr(layer, "activations"):
            raise ValueError(
                f"No activations detected for {type(layer)},"
                " run forward after add_hooks(model)"
            )
        if not hasattr(layer, "backprops_list"):
            raise ValueError(
                "No backprops detected, run backward after add_hooks(model)"
            )
        if len(layer.backprops_list) != 1:
            raise ValueError(
                "Multiple backprops detected, make sure to call clear_backprops(model)"
            )

        A = layer.activations
        n = A.shape[batch_dim]
        if loss_type == "mean":
            B = layer.backprops_list[0] * n
        else:  # loss_type == 'sum':
            B = layer.backprops_list[0]

        if batch_dim != 0:
            A = A.permute([batch_dim] + [x for x in range(A.dim()) if x != batch_dim])
            B = B.permute([batch_dim] + [x for x in range(B.dim()) if x != batch_dim])

        if layer_type == "Linear":
            gs = torch.einsum("n...i,n...j->n...ij", B, A)
            layer.weight.grad_sample = torch.einsum("n...ij->nij", gs)
            if layer.bias is not None:
                layer.bias.grad_sample = torch.einsum("n...k->nk", B)

        if layer_type == "LayerNorm":
            layer.weight.grad_sample = sum_over_all_but_batch_and_last_n(
                F.layer_norm(A, layer.normalized_shape, eps=layer.eps) * B,
                layer.weight.dim(),
            )
            layer.bias.grad_sample = sum_over_all_but_batch_and_last_n(
                B, layer.bias.dim()
            )

        if layer_type == "GroupNorm":
            gs = F.group_norm(A, layer.num_groups, eps=layer.eps) * B
            layer.weight.grad_sample = torch.einsum("ni...->ni", gs)
            if layer.bias is not None:
                layer.bias.grad_sample = torch.einsum("ni...->ni", B)

        elif layer_type in ("InstanceNorm1d", "InstanceNorm2d", "InstanceNorm3d"):
            gs = F.instance_norm(A, eps=layer.eps) * B
            layer.weight.grad_sample = torch.einsum("ni...->ni", gs)
            if layer.bias is not None:
                layer.bias.grad_sample = torch.einsum("ni...->ni", B)

        elif layer_type in ("Conv2d", "Conv1d"):
            # get A and B in shape depending on the Conv layer
            if layer_type == "Conv2d":
                A = torch.nn.functional.unfold(
                    A, layer.kernel_size, padding=layer.padding, stride=layer.stride
                )
                B = B.reshape(n, -1, A.shape[-1])
            elif layer_type == "Conv1d":
                # unfold doesn't work for 3D tensors; so force it to be 4D
                A = A.unsqueeze(-2)  # add the H dimension
                # set arguments to tuples with appropriate second element
                A = torch.nn.functional.unfold(
                    A,
                    (1, layer.kernel_size[0]),
                    padding=(0, layer.padding[0]),
                    stride=(1, layer.stride[0]),
                )
                B = B.reshape(n, -1, A.shape[-1])
            try:
                # n=batch_sz; o=num_out_channels; p=num_in_channels*kernel_sz
                grad_sample = (
                    torch.einsum("noq,npq->nop", B, A)
                    if layer.groups == 1
                    else torch.einsum("njk,njk->nj", B, A)
                )
                shape = [n] + list(layer.weight.shape)
                layer.weight.grad_sample = grad_sample.reshape(shape)
            except Exception as e:
                raise type(e)(
                    f"{e} There is probably a problem with {layer_type}.groups"
                    + "It should be either 1 or in_channel"
                )
            if layer.bias is not None:
                layer.bias.grad_sample = torch.sum(B, dim=2)
        if layer_type == "SequenceBias":
            layer.bias.grad_sample = B[:, -1]
