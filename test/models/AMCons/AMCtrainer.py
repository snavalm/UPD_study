# Add the parent directory to sys.path to allow importing from there
import sys
import os
sys.path.append(os.path.expanduser('~/thesis/UAD_study/'))
from argparse import ArgumentParser
from collections import defaultdict
from time import time
import numpy as np
from models import Encoder, Decoder
import torch
from torch import Tensor
from Utilities.common_config import common_config
from Utilities.utils import (save_model, seed_everything,
                             load_data, load_pretrained,
                             misc_settings, log, load_model)
from Utilities.evaluate import evaluate
from typing import Tuple
import torch.nn.functional as F
""""""""""""""""""""""""""""""""""" Config """""""""""""""""""""""""""""""""""


def get_config():
    parser = ArgumentParser()
    parser = common_config(parser)

    # Hyper-params training
    parser.add_argument("--lr", default=1e-4, type=float)
    parser.add_argument("--epochs", default=300, type=int)
    parser.add_argument("--batch_size", default=8, type=int)
    parser.add_argument('--max_steps', type=int, default=10000,
                        help='Number of training steps')

    # Auto-encoder architecture
    parser.add_argument("--zdim", default=32, type=int)
    parser.add_argument("--dense", default=True, type=bool)
    parser.add_argument("--n_blocks", default=4, type=int)

    # Settings with variational AE
    parser.add_argument("--wkl", default=10, type=float)

    # AMCons
    parser.add_argument("--wH", default=0.1, type=float, help='Alpha entropy')
    parser.add_argument("--level_cams", default=-4, type=float)

    return parser.parse_args()


config = get_config()
config.method = 'AMCons'
misc_settings(config)


""""""""""""""""""""""""""""""""" Load data """""""""""""""""""""""""""""""""

# specific seed for deterministic dataloader creation
seed_everything(42)

train_loader, val_loader, big_testloader, small_testloader = load_data(config)

""""""""""""""""""""""""""""""""" Init model """""""""""""""""""""""""""""""""
# Reproducibility
seed_everything(config.seed)

print("Initializing model...")

enc = Encoder(fin=3,
              zdim=config.zdim,
              dense=config.dense,
              n_blocks=config.n_blocks,
              spatial_dim=config.image_size // 2**config.n_blocks,
              variational=True,
              gap=False).to(config.device)

dec = Decoder(fin=config.zdim,
              nf0=enc.backbone.nfeats // 2,
              n_channels=config.img_channels,
              dense=config.dense,
              n_blocks=config.n_blocks,
              spatial_dim=config.image_size // 2**config.n_blocks,
              gap=False).to(config.device)

# Init optimizer
optimizer = torch.optim.Adam(list(enc.parameters()) + list(dec.parameters()), lr=config.lr)

# use this dict for saving
model = {'encoder': enc, 'decoder': dec}

# Load pre-trained backbone
if config.load_pretrained and not config.eval:
    config.arch = 'amc'
    enc = load_pretrained(enc, config)
    enc.eval()

# Load saved model to evaluate
if config.eval:
    load_enc, load_dec = load_model(config)
    enc.load_state_dict(load_enc)
    dec.load_state_dict(load_dec)
    print('Saved model loaded.')

if config.speed_benchmark:
    from torchinfo import summary
    input = next(iter(big_testloader))[0].cuda()

    a = summary(enc, (16, 3, 128, 128), verbose=0)
    b = summary(dec, enc(input)[0].shape, verbose=0)
    params = a.total_params + b.total_params
    macs = a.total_mult_adds + b.total_mult_adds
    print('Number of Million parameters: ', params / 1e06)
    print('Number of GMACs: ', macs / 1e09)


""""""""""""""""""""""""""""""""""" Training """""""""""""""""""""""""""""""""""


def loss_kl(mu, logvar):

    kl_divergence = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp())

    return kl_divergence


loss_r = torch.nn.BCEWithLogitsLoss(reduction='sum')


def train_step(input) -> dict:

    enc.train()
    dec.train()
    optimizer.zero_grad()

    # Forward pass
    z, z_mu, z_logvar, allF = enc(input)
    xhat, _ = dec(z)
    # Compute reconstruction and KL loss
    r_loss = loss_r(xhat, input) / config.batch_size
    kl_loss = loss_kl(mu=z_mu, logvar=z_logvar) / config.batch_size
    loss = r_loss + config.wkl * kl_loss

    # ---- Compute Attention Homogeneization loss via Entropy
    am = torch.mean(allF[config.level_cams], 1)
    # Restore original shape
    am = torch.nn.functional.interpolate(am.unsqueeze(1),
                                         size=(config.image_size, config.image_size),
                                         mode='bilinear',
                                         align_corners=True)

    # Probabilities
    p = torch.nn.functional.softmax(am.view((config.batch_size, -1)), dim=-1)
    # Mean entropy
    entropy_loss = torch.mean(-torch.sum(p * torch.log(p + 1e-12), dim=(-1)))
    loss += config.wH * entropy_loss
    loss.backward()
    optimizer.step()

    return {'loss': loss, 'rec_loss': r_loss, 'kl_loss': kl_loss, 'entropy_loss': entropy_loss}


def train():
    print('Starting training AMCons...')

    i_epoch = 0
    train_losses = defaultdict(list)
    t_start = time()

    while True:
        for input in train_loader:

            config.step += 1
            input = input.to(config.device)
            loss_dict = train_step(input)

            # Add to losses
            for k, v in loss_dict.items():
                train_losses[k].append(v.item())

            if config.step % config.log_frequency == 0:
                # Print training loss
                log_msg = " - ".join([f'{k}: {np.mean(v):.4f}' for k,
                                      v in train_losses.items()])
                log_msg = f"Iteration {config.step} - " + log_msg
                log_msg += f" - time: {time() - t_start:.2f}s"
                print(log_msg)

                # Log to wandb
                log(
                    {f'train/{k}': np.mean(v) for k, v in train_losses.items()},
                    config
                )

                # Reset
                train_losses = defaultdict(list)

            # if config.step % config.val_frequency == 0:
            #     validate(val_loader, config)

            if config.step % config.anom_val_frequency == 0:
                evaluate(config, small_testloader, val_step, val_loader)

            # if config.step % config.save_frequency == 0:
            #     save_model(e, config, i_iter=config.step)
            #     save_model(model, config, i_iter=config.step)

            if config.step >= config.max_steps:
                save_model(model, config)
                print(f'Reached {config.max_steps} iterations. Finished training {config.name}.')
                return

        i_epoch += 1
        # print(f'Finished epoch {i_epoch}, ({config.step} iterations)')


def val_step(input, return_loss: bool = True) -> Tuple[dict, Tensor]:
    enc.eval()
    dec.eval()

    # Get reconstruction error map
    z, _, _, f = enc(input)
    input_recon = torch.sigmoid(dec(z)[0]).detach()

    anom_map_small = torch.mean(f[config.level_cams], 1)
    # Restore original shape
    anomaly_map = F.interpolate(anom_map_small.unsqueeze(1),
                                size=(config.image_size, config.image_size),
                                mode='bilinear',
                                align_corners=True).detach()

    # for MRI apply brainmask
    if config.modality in ['MRI', 'CT']:
        mask = torch.stack([inp > inp.min() for inp in input])

        if config.get_images:
            anomaly_map *= mask
            mins = [(map[map > map.min()]) for map in anomaly_map]
            mins = [map.min() for map in mins]

            anomaly_map = torch.cat([(map - min) for map, min in zip(anomaly_map, mins)]).unsqueeze(1)

        anomaly_map *= mask
        anomaly_score = torch.tensor([map[inp > inp.min()].max() for map, inp in zip(anomaly_map, input)])

    elif config.modality in ['RF'] and config.dataset == 'DDR':
        anomaly_score = torch.tensor([map.max() for map in anomaly_map])
    else:
        anomaly_score = torch.tensor([map.mean() for map in anomaly_map])

    return anomaly_map, anomaly_score, input_recon


# def validate(val_loader, config) -> None:

#     val_losses = defaultdict(list)
#     i_val_step = 0

#     for input in val_loader:

#         i_val_step += 1

#         input = input.to(config.device)

#         loss_dict, anomaly_map, anomaly_score = val_step(input)

#         for k, v in loss_dict.items():
#             val_losses[k].append(v.item())

#         if i_val_step >= config.val_steps:
#             break

#     # Print validation results
#     log_msg = 'Validation losses on normal samples: '
#     log_msg += " - ".join([f'{k}: {np.mean(v):.4f}' for k, v in val_losses.items()])
#     print(log_msg)

#     # Log to wandb
#     log(
#         {f'val/{k}': np.mean(v)
#          for k, v in val_losses.items()},
#         config
#     )

#     # log images and residuals

#     log({
#         'val/input': input,
#         'val/res': anomaly_map,
#     }, config)

#     return np.mean(val_losses['rec_loss'])


if __name__ == '__main__':
    if config.eval:
        print('Evaluating model...')
        evaluate(config, big_testloader, val_step, val_loader)

    else:
        train()
