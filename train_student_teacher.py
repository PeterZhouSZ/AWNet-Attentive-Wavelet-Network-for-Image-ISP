# import pytorch dependencies
import torch
import torch.optim as optim
import torch.nn as nn
import torch.nn.init as init
import torch.nn.functional as F
import torch.optim.lr_scheduler as lr_scheduler
from torch.utils.data import DataLoader

import os
import time
import threading
from tensorboardX import SummaryWriter
import numpy as np
import shutil

from models_v1.model import Generator, teacher#, PixelDiscriminator
from loss import ms_Loss
from dataloader import LoadData, LoadVisualData
from config import trainConfig
from utils import validation, adjust_learning_rate, writer_add_image, print_log, to_psnr, poly_learning_decay, adjust_learning_rate_step

np.random.seed(0)
torch.manual_seed(0)

# Dataset size
TRAIN_SIZE = 46839
TEST_SIZE = 1204

def train():
    log_path = './runs_teacher'
    if os.path.isdir(log_path):
        shutil.rmtree(log_path)
    writer = SummaryWriter(log_path)
    if torch.cuda.device_count() > 0:
        device_ids = [1,0]
        print('using device: {}'.format(device_ids))
    else: device_ids = [1]
    device = torch.device("cuda:1" if torch.cuda.is_available() else "cpu")
    print("CUDA visible devices: " + str(torch.cuda.device_count()))
    print("CUDA Device Name: " + str(torch.cuda.get_device_name(device)))

    # Initialize loss and model
    loss = ms_Loss().to(device)
    net = Generator(4,3).to(device)
    net = nn.DataParallel(net, device_ids=device_ids)
    teacher_net = teacher(trainConfig.save_best, is_train=False).to(device)
    teacher_net = nn.DataParallel(teacher_net, device_ids=device_ids)


    new_lr = trainConfig.learning_rate[0]
    # Reload
    if trainConfig.pretrain == True:
        net.load_state_dict(torch.load('{}/student_best.pkl'.format(trainConfig.save_best))["model_state"])
        #pre_lr = torch.load('{}/matting_best.pkl'.format(trainConfig.save_best))["lr"]
        print('weight loaded.')
    else:
        # for m in net.modules():
        #     if isinstance(m, nn.Conv2d):
        #         init.kaiming_uniform_(m.weight)
        #         if m.bias is not None:
        #             nn.init.constant_(m.bias, 0)
        print('no weight loaded.')
    pytorch_total_params = sum(p.numel() for p in net.parameters() if p.requires_grad)
    print("Total_params: {}".format(pytorch_total_params))

    # optimizer and scheduler
    optimizer = torch.optim.Adam(net.parameters(), lr=new_lr, betas=(0.9, 0.999))
    scheduler = lr_scheduler.ExponentialLR(optimizer, gamma=0.95)

    # Dataloaders
    train_dataset = LoadData(trainConfig.data_dir, TRAIN_SIZE, dslr_scale=2, test=False)
    train_loader = DataLoader(dataset=train_dataset, batch_size=trainConfig.batch_size, shuffle=True, num_workers=24,
                              pin_memory=True, drop_last=True)

    test_dataset = LoadData(trainConfig.data_dir, TEST_SIZE, dslr_scale=2, test=True)
    test_loader = DataLoader(dataset=test_dataset, batch_size=6, shuffle=False, num_workers=18,
                             pin_memory=True, drop_last=False)

    # visual_dataset = LoadVisualData(trainConfig.data_dir, 10, scale=2, level=0)
    # visual_loader = DataLoader(dataset=visual_dataset, batch_size=1, shuffle=False, num_workers=0,
    #                            pin_memory=True, drop_last=False)
    print('Train loader length: {}'.format(len(train_loader)))
    

    pre_psnr, pre_ssim = 0, 0#validation(net, test_loader, device, save_tag=True)
    print(
        'previous PSNR: {:.4f}, previous ssim: {:.4f}'.format(pre_psnr, pre_ssim)
    )
    iteration = 0
    for epoch in range(trainConfig.epoch):
        psnr_list = []
        start_time = time.time() 
        # new_lr = adjust_learning_rate(optimizer, epoch, trainConfig.epoch, trainConfig.learning_rate)
        if epoch > 0:
        #     new_lr = adjust_learning_rate(optimizer, scheduler, epoch, trainConfig.learning_rate, writer)  
            new_lr = adjust_learning_rate_step(optimizer, epoch, trainConfig.epoch, trainConfig.learning_rate)

        for batch_id, data in enumerate(train_loader):
            x, target, _ = data
            x = x.to(device)
            target = target.to(device)
            latent = teacher_net(target)
            pred, latent_pred = net(x)

            
            optimizer.zero_grad()

            total_loss, losses = loss(pred, target)
            latent_loss = F.l1_loss(latent_pred, latent)
            total_loss += latent_loss
            total_loss.backward()
            optimizer.step()

            iteration += 1
            if trainConfig.print_loss:
                print("epoch:{}/{} | Loss: {:.4f} ".format(epoch, trainConfig.epoch, total_loss.item()))
            if not (batch_id % 1000):
                print('Epoch:{0}, Iteration:{1}'.format(epoch, batch_id))

            writer.add_scalars('data/train_loss_group',
                               {'g_loss': total_loss.item(),
                                # 'perceptual_loss': losses[0].item(),
                                'l1': losses[0].item(),
                                'ssim': losses[1].item(),
                                'latent': latent_loss.item(),
                                # 'tv': losses[3].item(),
                                }, iteration)

            psnr_list.extend(to_psnr(pred[0], target))

            if iteration % 100 == 0:
                threading.Thread(target=writer_add_image, args=('pred', writer, pred[0], iteration)).start()
                threading.Thread(target=writer_add_image, args=('target', writer, target, iteration)).start()
            del x, target, pred

        train_psnr = sum(psnr_list) / len(psnr_list)        
        state = {
                "model_state": net.state_dict(),
                "lr": new_lr,
            }
        print('saved checkpoint')
        torch.save(state, '{}/student_epoch_{}.pkl'.format(trainConfig.checkpoints, epoch))
        
        one_epoch_time = time.time() - start_time
        print('time: {}, train psnr: {}'.format(one_epoch_time, train_psnr))
        val_psnr, val_ssim = validation(net, test_loader, device, save_tag=True)
        print_log(epoch+1, trainConfig.epoch, one_epoch_time, train_psnr, val_psnr, val_ssim, 'multi_loss')

        writer.add_scalars('eval/validation_metrics_group',
                   {
                       'val psnr': val_psnr,
                        'val ssim': val_ssim, 
                   }, epoch)
        if val_psnr >= pre_psnr:
            state = {
                "model_state": net.state_dict(),
                "lr": new_lr,
            }

            print('saved best weight')
            torch.save(state, '{}/student_best.pkl'.format(trainConfig.save_best))
            pre_psnr = val_psnr

if __name__ == '__main__':
    train()