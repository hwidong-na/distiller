import torch
import torch.nn as nn
import torch.nn.functional as torch_func
from trainer import KDTrainer


def get_layer_types(feat_layers):
    conv_layers = []
    for layer in feat_layers:
        if not isinstance(layer, nn.Linear):
            conv_layers.append(layer)
    return conv_layers


def get_net_info(net, as_module=False):
    device = next(net.parameters()).device
    if isinstance(net, nn.DataParallel):
        net = net.module
    layers = list(net.children())
    feat_layers = get_layer_types(layers)
    linear = layers[-1]
    channels = []
    input_size = [[3, 32, 32]]
    x = [torch.rand(2, *in_size) for in_size in input_size]
    x = torch.Tensor(*x).to(device)
    for layer in feat_layers:
        x = layer(x)
        channels.append(x.shape)
    if as_module:
        return nn.ModuleList(feat_layers), linear, channels
    return feat_layers, linear, channels


def set_last_layers(linear, last_channel, as_module=False):
    # assume that h_in and w_in are equal...
    c_in = last_channel[1]
    h_in = last_channel[2]
    w_in = last_channel[3]
    flat_size = c_in * h_in * w_in
    pooling = int((flat_size / linear.in_features)**(0.5))
    modules = [nn.AvgPool2d((pooling)), nn.Flatten(), linear]
    if as_module:
        return nn.ModuleList(modules)
    return modules


def build_transformers(s_channels, t_channels):
    transfomers = []
    for idx, s_channel in enumerate(s_channels):
        t_channel = t_channels[idx]
        transformer = nn.Conv2d(s_channel[1], t_channel[1], kernel_size=1)
        transfomers.append(transformer)
    return nn.ModuleList(transfomers)


def get_layers(x, layers, classifier=[], use_relu=True):
    layer_feats = []
    out = x
    for layer in layers:
        out = layer(out)
        if use_relu:
            out = torch_func.relu(out)
        layer_feats.append(out)
    for last in classifier:
        out = last(out)
        layer_feats.append(out)
    return layer_feats[0], layer_feats[-1]


def compute_feature_loss(s_feats, t_feats, batch_size):
    feature_loss = 0.0
    s_totals = []
    t_totals = []
    for s_feat in s_feats:
        s_feat = torch_func.adaptive_max_pool2d(s_feat, (1, 1))
        s_totals.append(s_feat)
    for t_feat in t_feats:
        t_feat = torch_func.adaptive_max_pool2d(t_feat, (1, 1))
        t_totals.append(t_feat)
    s_total = torch.cat(s_totals, dim=1)
    t_total = torch.cat(t_totals, dim=1)
    feature_loss = torch_func.mse_loss(s_total, t_total)
    return feature_loss


class Distiller(nn.Module):
    def __init__(self, s_net, t_net):
        super(Distiller, self).__init__()

        self.s_feat_layers, self.s_linear, s_channels = get_net_info(
            s_net, as_module=True)
        self.t_feat_layers, self.t_linear, t_channels = get_net_info(
            t_net, as_module=False)
        self.s_last = set_last_layers(
            self.s_linear, s_channels[-1], as_module=True)
        self.t_last = set_last_layers(
            self.t_linear, t_channels[-1], as_module=False)

    def forward(self, x, targets=None, is_loss=False):
        s_feats, s_out = get_layers(x, self.s_feat_layers, self.s_last)
        if is_loss:
            batch_size = s_out.shape[0]
            t_feats, t_out = get_layers(x, self.t_feat_layers, self.t_last)
            feature_loss = 0.0
            feature_loss += compute_feature_loss(s_feats, t_feats, batch_size)
            return s_out, t_out, feature_loss
        return s_out


class FDTrainer(KDTrainer):

    def calculate_loss(self, data, target):
        s_out, t_out, feature_loss = self.net(data, target, is_loss=True)
        loss = 0.0
        # loss += self.loss_fun(s_out, target)
        loss += self.kd_loss(s_out, t_out, target)
        loss += feature_loss
        loss.backward()
        self.optimizer.step()
        return s_out, loss


def run_fd_distillation(s_net, t_net, **params):

    # Student training
    print("---------- Training FD Student -------")
    s_net = Distiller(s_net, t_net).to(params["device"])
    total_params = sum(p.numel() for p in s_net.parameters())
    print(f"FD distiller total parameters: {total_params}")
    s_trainer = FDTrainer(s_net, t_net, config=params)
    best_s_acc = s_trainer.train()

    return best_s_acc
