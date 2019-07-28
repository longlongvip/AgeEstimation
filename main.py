import os 
import time 
import json 
import argparse
import torch 
import torchvision
import random
import numpy as np 
from data import FaceDataset
from tqdm import tqdm 
from torch import nn
from torch import optim
from collections import OrderedDict
from torch.autograd import Variable
from torch.utils.data import DataLoader
from torch.optim import lr_scheduler
from torchvision.models.resnet import resnet34
from mean_variance_loss import MeanVarianceLoss

LAMBDA_1 = 0.2
LAMBDA_2 = 0.05
START_AGE = 0
END_AGE = 69
VALIDATION_RATE= 0.1

random.seed(2019)
np.random.seed(2019)
torch.manual_seed(2019)


def ResNet34(num_classes):

    model = resnet34(pretrained=True)
    model.fc = nn.Sequential(
        nn.BatchNorm1d(512),
        nn.Dropout(0.5),
        nn.Linear(512, num_classes),
    )
    return model


def train(train_loader, model, criterion1, criterion2, optimizer, epoch, result_directory):

    model.train()
    running_loss = 0.
    running_mean_loss = 0.
    running_variance_loss = 0.
    running_softmax_loss = 0.
    interval = 1
    for i, sample in enumerate(train_loader):
        images = sample['image'].cuda()
        labels = sample['label'].cuda()
        output = model(images)
        mean_loss, variance_loss = criterion1(output, labels)
        softmax_loss = criterion2(output, labels)
        loss = mean_loss + variance_loss + softmax_loss
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        running_loss += loss.data
        running_mean_loss += mean_loss.data
        running_variance_loss += variance_loss.data
        running_softmax_loss += softmax_loss.data
        if (i + 1) % interval == 0:
            print('[%d, %5d] mean_loss: %.3f, variance_loss: %.3f, softmax_loss: %.3f, loss: %.3f'
                  % (epoch, i, running_mean_loss / interval,
                     running_variance_loss / interval,
                     running_softmax_loss / interval,
                     running_loss / interval))
            with open(os.path.join(result_directory, 'log'), 'a') as f:
                f.write('[%d, %5d] mean_loss: %.3f, variance_loss: %.3f, softmax_loss: %.3f, loss: %.3f\n'
                        % (epoch, i, running_mean_loss / interval,
                           running_variance_loss / interval,
                           running_softmax_loss / interval,
                           running_loss / interval))
            running_loss = 0.
            running_mean_loss = 0.
            running_variance_loss = 0.
            running_softmax_loss = 0.


def evaluate(val_loader, model, criterion1, criterion2):
    model.cuda()
    model.eval()
    loss_val = 0.
    mean_loss_val = 0.
    variance_loss_val = 0.
    softmax_loss_val = 0.
    mae = 0.
    with torch.no_grad():
        for i, sample in enumerate(val_loader):
            image = sample['image'].cuda()
            label = sample['label'].cuda()
            output = model(image)
            m = nn.Softmax(dim=1)
            output = m(output)
            mean_loss, variance_loss = criterion1(output, label)
            softmax_loss = criterion2(output, label)
            loss = mean_loss + variance_loss + softmax_loss
            loss_val += loss.data
            mean_loss_val += mean_loss.data
            variance_loss_val += variance_loss.data
            softmax_loss_val += softmax_loss.data
            a = torch.arange(START_AGE, END_AGE + 1, dtype=torch.float32).cuda()
            mean = (output * a).sum(1, keepdim=True).cpu().data.numpy()
            pred = np.around(mean)
            mae += np.absolute(pred - sample['label'].cpu().data.numpy())
    return mean_loss_val / len(val_loader),\
        variance_loss_val / len(val_loader),\
        softmax_loss_val / len(val_loader),\
        loss_val / len(val_loader),\
        mae / len(val_loader)


def test(test_loader, model):
    model.cuda()
    model.eval()
    mae = 0.
    with torch.no_grad():
        for i, sample in enumerate(test_loader):
            image = sample['image'].cuda()
            label = sample['label'].cuda()
            output = model(image)
            m = nn.Softmax(dim=1)
            output = m(output)
            a = torch.arange(START_AGE, END_AGE + 1, dtype=torch.float32).cuda()
            mean = (output * a).sum(1, keepdim=True).cpu().data.numpy()
            pred = np.around(mean)
            mae += np.absolute(pred - sample['label'].cpu().data.numpy())
    return mae / len(test_loader)


def get_image_list(image_directory, leave_sub, validation_rate):
    
    train_val_list = []
    test_list = []
    for fn in os.listdir(image_directory):
        filepath = os.path.join(image_directory, fn)
        subject = int(fn[:3])
        if subject == leave_sub:
            test_list.append(filepath)
        else:
            train_val_list.append(filepath)
    num = len(train_val_list)
    index_val = np.random.choice(num, int(num * validation_rate), replace=False)
    train_list = []
    val_list = []
    for i, fp in enumerate(train_val_list):
        if i in index_val:
            val_list.append(fp)
        else:
            train_list.append(fp)

    return train_list, val_list, test_list


def get_args():

    parser = argparse.ArgumentParser()
    parser.add_argument('-b', '--batch_size', type=int, default=16)
    parser.add_argument('-i', '--image_directory', type=str)
    parser.add_argument('-ls', '--leave_subject', type=int)
    parser.add_argument('-lr', '--learning_rate', type=float)
    parser.add_argument('-e', '--epoch', type=int)
    parser.add_argument('-r', '--resume', type=str, default=None)
    parser.add_argument('-rd', '--result_directory', type=str, default=None)
    return parser.parse_args()


def main():

    args = get_args()
    batch_size = args.batch_size
    if not os.path.exists(args.result_directory):
        os.mkdir(args.result_directory)

    train_filepath_list, val_filepath_list, test_filepath_list\
        = get_image_list(args.image_directory, args.leave_subject, VALIDATION_RATE)
    transforms_train = torchvision.transforms.Compose([
        torchvision.transforms.ToPILImage(),
        torchvision.transforms.RandomApply(
            [torchvision.transforms.RandomAffine(degrees=10, shear=16),
             torchvision.transforms.RandomHorizontalFlip(p=1.0),
            ], p=0.5),
        torchvision.transforms.Resize((256, 256)),
        torchvision.transforms.RandomCrop((224, 224)),
        torchvision.transforms.ToTensor()
    ])
    train_gen = FaceDataset(train_filepath_list, transforms_train)
    train_loader = DataLoader(train_gen, batch_size=batch_size, shuffle=True, pin_memory=True, num_workers=8)

    transforms = torchvision.transforms.Compose([
        torchvision.transforms.ToPILImage(),
        torchvision.transforms.Resize((224, 224)),
        torchvision.transforms.ToTensor()
    ])
    val_gen = FaceDataset(val_filepath_list, transforms)
    val_loader = DataLoader(val_gen, batch_size=1, shuffle=False, pin_memory=True, num_workers=8)

    test_gen = FaceDataset(test_filepath_list, transforms)
    test_loader = DataLoader(test_gen, batch_size=1, shuffle=False, pin_memory=True, num_workers=8)

    model = ResNet34(END_AGE - START_AGE + 1)
    model.cuda()

    optimizer = optim.SGD(model.parameters(), lr = args.learning_rate, momentum=0.9, weight_decay=1e-4)
    criterion1 = MeanVarianceLoss(LAMBDA_2, LAMBDA_1, START_AGE, END_AGE).cuda()
    criterion2 = torch.nn.CrossEntropyLoss().cuda()

    # scheduler = lr_scheduler.StepLR(optimizer, step_size=15, gamma=0.1)
    scheduler = lr_scheduler.MultiStepLR(optimizer, milestones=[120], gamma=0.1)

    for param in model.parameters():
        param.requires_grad = False
    for param in model.fc.parameters():
        param.requires_grad = True

    best_val_mae = np.inf
    best_val_loss = np.inf
    best_mae_epoch = -1
    best_loss_epoch = -1
    for epoch in range(args.epoch):
        if epoch == 10:
            for param in model.parameters():
                param.requires_grad = True
        scheduler.step(epoch)
        train(train_loader, model, criterion1, criterion2, optimizer, epoch, args.result_directory)
        mean_loss, variance_loss, softmax_loss, loss_val, mae = evaluate(val_loader, model, criterion1, criterion2)
        mae_test = test(test_loader, model)
        print('epoch: %d, mean_loss: %.3f, variance_loss: %.3f, softmax_loss: %.3f, loss: %.3f, mae: %3f' %
              (epoch, mean_loss, variance_loss, softmax_loss, loss_val, mae))
        print('epoch: %d, test_mae: %3f' % (epoch, mae_test))
        with open(os.path.join(args.result_directory, 'log'), 'a') as f:
            f.write('epoch: %d, mean_loss: %.3f, variance_loss: %.3f, softmax_loss: %.3f, loss: %.3f, mae: %3f\n' %
                    (epoch, mean_loss, variance_loss, softmax_loss, loss_val, mae))
            f.write('epoch: %d, mae_test: %3f\n' % (epoch, mae_test))
        if best_val_mae > mae:
            best_val_mae = mae
            best_mae_epoch = epoch
            torch.save(model.state_dict(), os.path.join(args.result_directory, "model_best_mae"))
        if best_val_loss > loss_val:
            best_val_loss = loss_val
            best_loss_epoch = epoch
            torch.save(model.state_dict(), os.path.join(args.result_directory, "model_best_loss"))            
    with open(os.path.join(args.result_directory, 'log'), 'a') as f:
        f.write('best_loss_epoch: %d, best_val_loss: %f, best_mae_epoch: %d, best_val_mae: %f\n'
                % (best_loss_epoch, best_val_loss, best_mae_epoch, best_val_mae))
    print('best_loss_epoch: %d, best_val_loss: %f, best_mae_epoch: %d, best_val_mae: %f'
          % (best_loss_epoch, best_val_loss, best_mae_epoch, best_val_mae))

if __name__ == "__main__":
    main()