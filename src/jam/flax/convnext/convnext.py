# type: ignore
import dataclasses
import functools
from typing import Any, List

import flax
from flax import linen as nn
import jax
import jax.numpy as jnp
import numpy as np

from jam.utils import checkpoint_utils


class StochDepth(nn.Module):
    """Batchwise Dropout used in EfficientNet, optionally sans rescaling."""

    drop_rate: float
    scale_by_keep: bool = False
    rng_collection: str = "dropout"

    def __call__(self, x, is_training) -> jnp.ndarray:
        if not is_training:
            return x
        batch_size = x.shape[0]
        rng = self.make_rng(self.rng_collection)
        r = jax.random.uniform(rng, [batch_size, 1, 1, 1], dtype=x.dtype)
        keep_prob = 1.0 - self.drop_rate
        binary_tensor = jnp.floor(keep_prob + r)
        if self.scale_by_keep:
            x = x / keep_prob
        return x * binary_tensor


@dataclasses.dataclass
class CNBlockConfig:
    num_channels: int
    num_layers: int


class CNBlock(nn.Module):
    dim: int
    layer_scale: float
    stochastic_depth_prob: float
    norm_cls: Any = functools.partial(nn.LayerNorm, epsilon=1e-6)

    def setup(self) -> None:
        self.block = nn.Sequential(
            [
                nn.Conv(
                    features=self.dim,
                    kernel_size=(7, 7),
                    padding=3,
                    feature_group_count=self.dim,
                    use_bias=True,
                ),
                self.norm_cls(),
                nn.Dense(4 * self.dim, use_bias=True),
                jax.nn.gelu,
                nn.Dense(self.dim, use_bias=True),
            ]
        )
        self.layer_scale_param = self.param(
            "layer_scale",
            lambda key, shape, dtype: jnp.full(shape, self.layer_scale, dtype),
            (self.dim,),
            jnp.float32,
        )
        self.stoch_depth = StochDepth(self.stochastic_depth_prob)

    def __call__(self, inputs: jnp.ndarray, is_training) -> None:
        result = self.layer_scale_param * self.block(inputs)
        result = self.stoch_depth(result, is_training)
        result = inputs + result
        return result


class ConvNextStage(nn.Module):
    num_channels: int
    num_layers: int
    layer_scale: float
    stochastic_depth_probs: List[float]
    stride: int = 1
    block_cls: Any = CNBlock
    norm_cls: Any = functools.partial(nn.LayerNorm, epsilon=1e-6)

    @nn.compact
    def __call__(self, inputs, is_training) -> Any:
        x = inputs
        if inputs.shape[-1] != self.num_channels or self.stride != 1:
            downsample = nn.Sequential(
                [
                    self.norm_cls(),
                    nn.Conv(self.num_channels, kernel_size=(2, 2), strides=self.stride),
                ]
            )
            x = downsample(x)

        blocks = []
        for i in range(self.num_layers):
            blocks.append(
                self.block_cls(
                    self.num_channels, self.layer_scale, self.stochastic_depth_probs[i]
                )
            )
        for block in blocks:
            x = block(x, is_training)

        return x


class ConvNeXt(nn.Module):
    block_settings: List[CNBlockConfig]
    block_cls: Any = CNBlock
    stochastic_depth_prob: float = 0.0
    layer_scale: float = 1e-6
    num_classes: int = 1000
    block_cls: Any = CNBlock
    norm_cls: Any = functools.partial(nn.LayerNorm, epsilon=1e-6)

    def setup(self) -> None:
        block_setting = self.block_settings

        firstconv_output_channels = block_setting[0].num_channels
        stem = nn.Sequential(
            [
                nn.Conv(
                    firstconv_output_channels,
                    kernel_size=(4, 4),
                    strides=4,
                    padding=0,
                    use_bias=True,
                ),
                self.norm_cls(),
            ]
        )
        self.stem = stem

        total_stage_blocks = sum(cnf.num_layers for cnf in block_setting)
        sd_probs = [
            self.stochastic_depth_prob * stage_block_id / (total_stage_blocks - 1.0)
            for stage_block_id in range(total_stage_blocks)
        ]
        sd_probs = np.split(
            sd_probs, np.cumsum([cnf.num_layers for cnf in block_setting])
        )

        stages = []
        for i, cnf in enumerate(block_setting):
            stages.append(
                ConvNextStage(
                    cnf.num_channels,
                    cnf.num_layers,
                    self.layer_scale,
                    stride=2 if i > 0 else 1,
                    stochastic_depth_probs=sd_probs[i],
                    block_cls=self.block_cls,
                    norm_cls=self.norm_cls,
                )
            )

        self.stages = stages
        self.classifier = nn.Sequential(
            [
                self.norm_cls(),
                lambda x: jnp.reshape(x, (x.shape[0], -1)),
                nn.Dense(self.num_classes),
            ],
            name="classifier",
        )

    @nn.compact
    def __call__(self, inputs, is_training: bool = True) -> jnp.ndarray:
        x = self.stem(inputs)
        for stage in self.stages:
            x = stage(x, is_training)
        x = jnp.mean(x, axis=(1, 2), keepdims=True)
        x = self.classifier(x)
        return x


def convnext_tiny():
    block_setting = [
        CNBlockConfig(96, 3),
        CNBlockConfig(192, 3),
        CNBlockConfig(384, 9),
        CNBlockConfig(768, 3),
    ]
    stocharstic_depth_prob = 0.1
    return ConvNeXt(block_setting, stochastic_depth_prob=stocharstic_depth_prob)


def convnext_small():
    block_setting = [
        CNBlockConfig(96, 3),
        CNBlockConfig(192, 3),
        CNBlockConfig(384, 27),
        CNBlockConfig(768, 3),
    ]
    stocharstic_depth_prob = 0.4
    return ConvNeXt(block_setting, stochastic_depth_prob=stocharstic_depth_prob)


def convnext_base():
    block_setting = [
        CNBlockConfig(128, 3),
        CNBlockConfig(256, 3),
        CNBlockConfig(512, 27),
        CNBlockConfig(1024, 3),
    ]
    stocharstic_depth_prob = 0.5
    return ConvNeXt(block_setting, stochastic_depth_prob=stocharstic_depth_prob)


def convnext_large():
    block_setting = [
        CNBlockConfig(192, 3),
        CNBlockConfig(384, 3),
        CNBlockConfig(768, 27),
        CNBlockConfig(1536, 3),
    ]
    stocharstic_depth_prob = 0.5
    return ConvNeXt(block_setting, stochastic_depth_prob=stocharstic_depth_prob)


convnext_importer = checkpoint_utils.CheckpointTranslator()


def transpose_conv_weights(w):
    return np.transpose(w, [2, 3, 1, 0])


@convnext_importer.add(r"features.0.0.(weight|bias)")
def initial_conv(key, val, weight_or_bias):
    newname = {"weight": "kernel", "bias": "bias"}[weight_or_bias]
    newkey = f"stem/layers_0/{newname}"
    if newname == "kernel":
        val = transpose_conv_weights(val)
    return newkey, val


@convnext_importer.add(r"features.0.1.(weight|bias)")
def initial_norm(key, val, weight_or_bias):
    newname = {"weight": "scale", "bias": "bias"}[weight_or_bias]
    newkey = f"stem/layers_1/{newname}"
    return newkey, val


@convnext_importer.add(r"features.(1|3|5|7).(\d+).layer_scale")
def cn_block_layer_scale(key, val, stage, block_id):
    new_stage = (int(stage) - 1) // 2
    newkey = f"stages_{new_stage}/CNBlock_{block_id}/layer_scale"
    return newkey, val.reshape(-1)


@convnext_importer.add(r"features.(1|3|5|7).(\d+).block.0.(weight|bias)")
def cn_block_block_conv(key, val, stage, block, weight_or_bias):
    newname = {"weight": "kernel", "bias": "bias"}[weight_or_bias]
    new_stage = (int(stage) - 1) // 2
    newkey = f"stages_{new_stage}/CNBlock_{block}/block/layers_0/{newname}"
    if weight_or_bias == "weight":
        val = transpose_conv_weights(val)
    return newkey, val


@convnext_importer.add(r"features.(1|3|5|7).(\d+).block.(3|5).(weight|bias)")
def cn_block_block_dense(key, val, stage, block, dense_idx, weight_or_bias):
    new_idx = {3: 2, 5: 4}[int(dense_idx)]
    newname = {"weight": "kernel", "bias": "bias"}[weight_or_bias]
    new_stage = (int(stage) - 1) // 2
    newkey = f"stages_{new_stage}/CNBlock_{block}/block/layers_{new_idx}/{newname}"
    if weight_or_bias == "weight":
        val = np.transpose(val, [1, 0])
    return newkey, val


@convnext_importer.add(r"features.(1|3|5|7).(\d+).block.2.(weight|bias)")
def cn_block_block_norm(key, val, stage, block, weight_or_bias):
    newname = {"weight": "scale", "bias": "bias"}[weight_or_bias]
    new_stage = (int(stage) - 1) // 2
    newkey = f"stages_{new_stage}/CNBlock_{block}/block/layers_1/{newname}"
    return newkey, val


@convnext_importer.add(r"features.(2|4|6).0.(weight|bias)")
def block_projection_norm(key, val, layer, weight_or_bias):
    newname = {"weight": "scale", "bias": "bias"}[weight_or_bias]
    newkey = f"stages_{int(layer) // 2}/LayerNorm_0/{newname}"
    return newkey, val


@convnext_importer.add(r"features.(2|4|6).1.(weight|bias)")
def block_projection_conv(key, val, layer, weight_or_bias):
    newname = {"weight": "kernel", "bias": "bias"}[weight_or_bias]
    newkey = f"stages_{int(layer) // 2}/Conv_0/{newname}"
    if weight_or_bias == "weight":
        val = transpose_conv_weights(val)
    return newkey, val


@convnext_importer.add(r"classifier.0.(weight|bias)")
def classifier_norm(key, val, weight_or_bias):
    newname = {"weight": "scale", "bias": "bias"}[weight_or_bias]
    newkey = f"classifier/layers_0/{newname}"
    return newkey, val


@convnext_importer.add(r"classifier.2.(weight|bias)")
def classifier_dense(key, val, weight_or_bias):
    newname = {"weight": "kernel", "bias": "bias"}[weight_or_bias]
    newkey = f"classifier/layers_2/{newname}"
    if weight_or_bias == "weight":
        val = np.transpose(val, [1, 0])
    return newkey, val


def load_from_torch_checkpoint(state_dict):
    converted_dict = convnext_importer.apply(
        state_dict=checkpoint_utils.as_numpy(state_dict)
    )
    converted_dict = {k: v for k, v in converted_dict.items()}
    return {"params": flax.traverse_util.unflatten_dict(converted_dict, "/")}