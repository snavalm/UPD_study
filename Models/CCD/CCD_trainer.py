import argparse
import torch
import numpy as np
import torch.nn as nn
from models import ContrastiveModel
from losses import SimCLRLoss
from collections import defaultdict
from time import time
from Utilities.common_config import common_config
from Utilities.utils import (save_model, seed_everything, misc_settings, str_to_bool)


""""""""""""""""""""""""""""""""""" Config """""""""""""""""""""""""""""""""""


def get_config():
    parser = argparse.ArgumentParser()
    parser = common_config(parser)

    parser.add_argument('--only_simclr', type=int, default=128, help='Train only with the SimClr task')
    parser.add_argument('--backbone-arch', type=str, default='wide_resnet50_2', help='Backbone architecture.',
                        choices=['resnet18', 'resnet50', 'wide_resnet50_2', 'vae', 'fanogan', 'vgg19'])
    parser.add_argument('--cls-head-number', default=2, type=int)
    parser.add_argument('--lr', type=float, default=0.01, help='Learning rate')
    parser.add_argument('--max-epochs', type=int, default=200, help='Number of training epochs')
    parser.add_argument('--max-steps', type=int, default=20000, help='Number of training steps')
    parser.add_argument('--save-checkpoint', type=int, default=2000, help='Checkpoint save frequency')
    parser.add_argument('--log-frequency', default=200, type=int)

    # VAE Hyperparameters
    parser.add_argument('--latent_dim', type=int, default=512, help='Model width')
    parser.add_argument('--num_layers', type=int, default=6, help='Model width')
    parser.add_argument('--width', type=int, default=16, help='First conv layer num of filters')
    parser.add_argument('--conv1x1', type=int, default=16,
                        help='Channel downsampling with 1x1 convs before bottleneck')
    parser.add_argument('--kernel_size', type=int, default=3, help='convolutional kernel size')
    parser.add_argument('--padding', type=int, default=1,
                        help='padding for consistent downsampling, set 2 if kernel_size == 5')
    parser.add_argument('--kl_weight', type=float, default=0.001, help='KL loss term weight')
    parser.add_argument('--dropout', type=float, default=0.0,
                        help='Input Dropout like https://doi.org/10.1145/1390156.1390294')

    config = parser.parse_args()
    return config


config = get_config()

if config.backbone_arch == 'fanogan':
    config.latent_dim = 128

# get logger and naming string
config.method = 'CCD'
config.naming_str, _ = misc_settings(config)


""""""""""""""""""""""""""""""""" Load data """""""""""""""""""""""""""""""""

print('Loading dataset...')

if config.modality == 'COL':
    from Models.CCD.datasets.COL_CCD import get_train_dataloader
elif config.modality == 'MRI':
    from Models.CCD.datasets.MRI_CCD import get_train_dataloader
elif config.modality == 'CXR':
    from Models.CCD.datasets.CXR_CCD import get_train_dataloader
elif config.modality == 'CXR':
    from Models.CCD.datasets.CXR_CCD import get_train_dataloader

train_dataloader = get_train_dataloader(config)

""""""""""""""""""""""""""""""""" Init model """""""""""""""""""""""""""""""""
# Reproducibility
seed_everything(config.seed)

# Init model
print("Initializing model...")
model = ContrastiveModel(config, features_dim=128).to(config.device)

# Loss, Optimizer
criterion = SimCLRLoss(temperature=0.2)
optimizer = torch.optim.SGD(model.parameters(), lr=config.lr, weight_decay=0.0003, momentum=0.9)

# Initiate CELoss module
celoss = nn.CrossEntropyLoss()


def adjust_lr(lr, optimizer, epoch, max_epochs):
    eta_min = lr * (0.1 ** 3)
    lr = eta_min + (lr - eta_min) * (1 + np.cos(np.pi * epoch / max_epochs)) / 2
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return lr


def train():
    # Training
    start_epoch = 0
    i_iter = 0
    lr = config.lr
    train_losses = defaultdict(list)
    t_start = time()

    print('Starting pre-training with CCD...')
    for epoch in range(start_epoch, config.max_epochs):

        # Adjust lr (cosine annealing)
        lr = adjust_lr(lr, optimizer, epoch, config.max_epochs)

        for i, batch in enumerate(train_dataloader):

            i_iter += 1
            loss_dict = train_step(batch)

            # Accumulate losses
            for k, v in loss_dict.items():
                train_losses[k].append(v.item())

            # Log losses
            if i_iter % config.log_frequency == 0:
                # Print training loss
                log_msg = " - ".join([f'{k}: {np.mean(v):.4f}' for k,
                                      v in train_losses.items()])
                log_msg = f"Iteration {i_iter} - Epoch {epoch} - " + log_msg
                log_msg += f" - time: {time() - t_start:.2f}s"
                log_msg += f" - learning rate: {lr:6f}"
                print(log_msg)

                # Reset loss dict
                train_losses = defaultdict(list)

            if i_iter % config.save_checkpoint == 0:
                save_model(model.backbone, config)

            if i_iter % config.max_steps == 0:
                print(f'Reached {config.max_steps} iterations. Finished pre-training with CCD.')
                save_model(model.backbone, config)
                return

    print(f'Reached { config.max_epochs} epochs. Finished pre-training with CCD.')
    save_model(model.backbone, config)


def train_step(batch):
    """
    Train according to the scheme from SimCLR
    https://arxiv.org/abs/2002.05709
    """
    model.train()
    optimizer.zero_grad()

    # get current batch
    images = batch['image']
    images_augmented = batch['image_augmented']
    b, c, h, w = images.size()

    # combine α(x) and α'(x)
    input_ = torch.cat([images.unsqueeze(1), images_augmented.unsqueeze(1)], dim=1)
    input_ = input_.view(-1, c, h, w)

    input_ = input_.to(config.device)
    output, logits = model(input_)
    output = output.view(b, 2, -1)
    labels = batch['target']

    labels = labels.to(config.device)
    labels = labels.repeat(2)

    loss_cla = celoss(logits, labels)
    loss_con = criterion(output)

    if config.only_simclr:

        loss_con.backward()
        optimizer.step()
        return {'loss_con': loss_con}

    else:
        loss = loss_con + loss_cla
        loss.backward()
        optimizer.step()
        return {'loss_cla': loss_cla, 'loss_con': loss_con, 'loss': loss}


if __name__ == '__main__':
    train()
