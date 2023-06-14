import numpy as np
from einops import rearrange, repeat
from timm import create_model
import torch
from torch import nn, Tensor
import torch.nn.functional as F

from config import Model
from helper_classes.feature_pooling import FeaturePooling


# model here: https://github.com/huggingface/pytorch-image-models/blob/b3e816d6d71ec132b39c603d68b619ae2870fd0a/timm/models/convnext.py#L410
# lr=4.0e-3 (mentioned in  A ConvNet for the 2020s paper)


class TemplateMixingConvNeXt(nn.Module):
    def __init__(self, config: Model):
        # pretrained_model_name "convnext_tiny.fb_in22k"
        ## forward pass: https://github.com/huggingface/pytorch-image-models/blob/b3e816d6d71ec132b39c603d68b619ae2870fd0a/timm/models/convnext.py#L420

        super().__init__()
        self.cfg = config
        model = create_model(config.pretrained_model_name, pretrained=config.pretrained)

        num_templates = config.num_templates

        ## all channels in this order (alphabet): config.in_channel_names = ['er', 'golgi', 'membrane', 'microtubules','mito','nucleus','protein', 'rna']

        self.mapper = {
            "Allen": [5, 2, 6],
            "HPA": [3, 6, 5, 0],
            "CP": [5, 0, 7, 1, 4],
        }

        out_dim, original_in_dim, kh, kw = model.stem[0].weight.shape
        self.stride = model.stem[0].stride
        self.padding = model.stem[0].padding
        self.dilation = model.stem[0].dilation
        self.groups = model.stem[0].groups

        # First conv layer
        self.conv1_param_bank = nn.Parameter(
            torch.zeros(out_dim, num_templates, kh, kw)
        )
        if self.cfg.separate_coef:
            self.conv1_coefs = nn.ParameterDict(
                {
                    data_channel: nn.Parameter(
                        torch.zeros(len(channels), num_templates)
                    )
                    for data_channel, channels in self.mapper.items()
                }
            )
        else:
            self.conv1_coefs = nn.Parameter(
                torch.zeros(len(config.in_channel_names), num_templates)
            )

        nn.init.kaiming_normal_(
            self.conv1_param_bank, mode="fan_in", nonlinearity="relu"
        )
        if isinstance(self.conv1_coefs, nn.ParameterDict):
            for param in self.conv1_coefs.values():
                nn.init.orthogonal_(param)
        else:
            nn.init.orthogonal_(self.conv1_coefs)

        ## Make a list to store reference for easy access later on
        self.adaptive_interface = nn.ParameterList(
            [self.conv1_param_bank, self.conv1_coefs]
        )

        ## shared feature_extractor
        self.feature_extractor = nn.Sequential(
            model.stem[1],
            model.stages[0],
            model.stages[1],
            model.stages[2].downsample,
            *[model.stages[2].blocks[i] for i in range(9)],
            model.stages[3].downsample,
            *[model.stages[3].blocks[i] for i in range(3)],
        )

        num_proxies = (
            config.num_classes
        )  ## depends on the number of classes of the dataset
        self.dim = 768 if self.cfg.pooling in ["avg", "max", "avgmax"] else 7 * 7 * 768
        self.proxies = torch.nn.Parameter((torch.randn(num_proxies, self.dim) / 8))
        init_temperature = config.temperature  # scale = sqrt(1/T)
        if self.cfg.learnable_temp:
            self.logit_scale = nn.Parameter(
                torch.ones([]) * np.log(1 / init_temperature)
            )
        else:
            self.scale = np.sqrt(1.0 / init_temperature)

    def mix_templates_first_layer(self, chunk: str) -> Tensor:
        """
        @return: return a tensor, shape (out_channels, in_channels, kernel_h, kernel_w)
        """
        assert chunk in self.mapper, f"Invalid chunk: {chunk}"
        if self.cfg.separate_coef:
            coefs = self.conv1_coefs[chunk]
        else:
            coefs = self.conv1_coefs[self.mapper[chunk]]

        coefs = rearrange(coefs, "c t ->1 c t 1 1")
        templates = repeat(
            self.conv1_param_bank, "o t h w -> o c t h w", c=len(self.mapper[chunk])
        )
        params = torch.sum(coefs * templates, dim=2)
        return params

    def _reset_params(self, model):
        for m in model.children():
            if len(list(m.children())) > 0:
                self._reset_params(m)

            elif isinstance(m, nn.Conv2d):
                print("resetting", m)
                nn.init.kaiming_normal_(m.weight, mode="fan_out", nonlinearity="relu")
                if m.bias is not None:
                    nn.init.zeros_(m.bias)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.ones_(m.weight)
                nn.init.zeros_(m.bias)
                print("resetting", m)

            elif isinstance(m, nn.Linear):
                print("resetting", m)

                nn.init.normal_(m.weight, 0, 0.01)
                nn.init.zeros_(m.bias)
            else:
                print("skipped", m)

    def _init_bias(self, model):
        ## Init bias of the first layer
        if model.stem[0].bias is not None:
            fan_in, _ = nn.init._calculate_fan_in_and_fan_out(model.stem[0].weight)
            bound = 1 / np.sqrt(fan_in)
            nn.init.uniform_(model.stem[0].bias, -bound, bound)

    def forward(self, x: torch.Tensor, chunk: str) -> torch.Tensor:
        conv1_params = self.mix_templates_first_layer(chunk)
        x = F.conv2d(
            x,
            conv1_params,
            bias=None,
            stride=self.stride,
            padding=self.padding,
            dilation=self.dilation,
            groups=self.groups,
        )

        x = self.feature_extractor(x)
        if self.cfg.pooling == FeaturePooling.AVG:
            x = F.adaptive_avg_pool2d(x, (1, 1))
        elif self.cfg.pooling == FeaturePooling.MAX:
            x = F.adaptive_max_pool2d(x, (1, 1))
        elif self.cfg.pooling == FeaturePooling.AVG_MAX:
            x_avg = F.adaptive_avg_pool2d(x, (1, 1))
            x_max = F.adaptive_max_pool2d(x, (1, 1))
            x = torch.cat([x_avg, x_max], dim=1)
        elif self.cfg.pooling == FeaturePooling.NONE:
            pass
        else:
            raise ValueError(
                f"Pooling {self.cfg.pooling} not supported. Use one of {FeaturePooling.list()}"
            )
        x = rearrange(x, "b c h w -> b (c h w)")
        return x


def templatemixingconvnext(cfg: Model, **kwargs) -> TemplateMixingConvNeXt:
    return TemplateMixingConvNeXt(config=cfg)
