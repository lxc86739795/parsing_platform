import argparse

import torch
torch.multiprocessing.set_start_method("spawn", force=True)
from torch.utils import data
import numpy as np
import torch.optim as optim
import torchvision.utils as vutils
import time
import torch.backends.cudnn as cudnn
import os
import os.path as osp
import sys
sys.path.append('../../')  

from networks.hrnet_v2_synbn import get_cls_net
from dataset.datasets import LIPDataSet, VPDataSet, WYDataSet
import torchvision.transforms as T
import timeit
from tensorboardX import SummaryWriter
from utils.utils import decode_parsing, inv_preprocess
from utils.criterion import CriterionAll
from utils.loss import OhemCrossEntropy2d
from utils.encoding import DataParallelModel, DataParallelCriterion 
from utils.miou import compute_mean_ioU
from config import config
from config import update_config

start = timeit.default_timer()
 
BATCH_SIZE = 8
DATA_DIRECTORY = 'cityscapes'
DATA_LIST_PATH = './dataset/list/cityscapes/train.lst'
IGNORE_LABEL = 255
INPUT_SIZE = '769,769'
LEARNING_RATE = 1e-2
MOMENTUM = 0.9
NUM_CLASSES = 20
POWER = 0.9
RANDOM_SEED = 618
RESTORE_FROM = './dataset/MS_DeepLab_resnet_pretrained_init.pth'
SAVE_NUM_IMAGES = 2
SAVE_PRED_EVERY = 10000
SNAPSHOT_DIR = './snapshots/'
WEIGHT_DECAY = 0.0005
 
def str2bool(v):
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


def get_arguments():
    """Parse all the arguments provided from the CLI.

    Returns:
      A list of parsed arguments.
    """
    parser = argparse.ArgumentParser(description="HRNET Network")
    parser.add_argument('--cfg',default='cls_hrnet_w48_sgd_lr5e-2_wd1e-4_bs32_x100.yaml',
                        help='experiment configure file name',
                        #required=True,
                        type=str)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help="Number of images sent to the network in one step.")
    parser.add_argument("--data-dir", type=str, default=DATA_DIRECTORY,
                        help="Path to the directory containing the dataset.")
    parser.add_argument("--dataset", type=str, default='train', choices=['train', 'test', 'test_no_label'],
                        help="Path to the file listing the images in the dataset.")
    parser.add_argument("--ignore-label", type=int, default=IGNORE_LABEL,
                        help="The index of the label to ignore during the training.")
    parser.add_argument("--input-size", type=str, default=INPUT_SIZE,
                        help="Comma-separated string with height and width of images.")
    parser.add_argument("--learning-rate", type=float, default=LEARNING_RATE,
                        help="Base learning rate for training with polynomial decay.")
    parser.add_argument("--momentum", type=float, default=MOMENTUM,
                        help="Momentum component of the optimiser.")
    parser.add_argument("--num-classes", type=int, default=NUM_CLASSES,
                        help="Number of classes to predict (including background).") 
    parser.add_argument("--start-iters", type=int, default=0,
                        help="Number of classes to predict (including background).") 
    parser.add_argument("--power", type=float, default=POWER,
                        help="Decay parameter to compute the learning rate.")
    parser.add_argument("--weight-decay", type=float, default=WEIGHT_DECAY,
                        help="Regularisation parameter for L2-loss.")
    parser.add_argument("--random-mirror", action="store_true",
                        help="Whether to randomly mirror the inputs during the training.")
    parser.add_argument("--random-scale", action="store_true",
                        help="Whether to randomly scale the inputs during the training.")
    parser.add_argument("--random-seed", type=int, default=RANDOM_SEED,
                        help="Random seed to have reproducible results.")
    parser.add_argument("--restore-from", type=str, default=RESTORE_FROM,
                        help="Where restore model parameters from.")
    parser.add_argument("--save-num-images", type=int, default=SAVE_NUM_IMAGES,
                        help="How many images to save.")
    parser.add_argument("--snapshot-dir", type=str, default=SNAPSHOT_DIR,
                        help="Where to save snapshots of the model.")
    parser.add_argument("--gpu", type=str, default='None',
                        help="choose gpu device.")
    parser.add_argument("--list_path", type=str, default='None',
                        help="choose gpu device.")
    parser.add_argument("--start-epoch", type=int, default=0,
                        help="choose the number of recurrence.")
    parser.add_argument("--save_step", type=int, default=0,
                        help="choose the number of recurrence.")
    parser.add_argument("--epochs", type=int, default=150,
                        help="choose the number of recurrence.")
    parser.add_argument("--loss", type=str, default='softmax',
                        help="")
    return parser.parse_args()


args = get_arguments()
update_config(config, args)


def lr_poly(base_lr, iter, max_iter, power):
    return base_lr * ((1 - float(iter) / max_iter) ** (power))


def adjust_learning_rate(optimizer, i_iter, total_iters):
    """Sets the learning rate to the initial LR divided by 5 at 60th, 120th and 160th epochs"""
    lr = lr_poly(args.learning_rate, i_iter, total_iters, args.power)
    optimizer.param_groups[0]['lr'] = lr
    # for i in range(1,len( optimizer.param_groups)):
        # optimizer.param_groups[i]['lr'] = lr
    return lr


def set_bn_eval(m):
    classname = m.__class__.__name__
    if classname.find('BatchNorm') != -1:
        m.eval()


def set_bn_momentum(m):
    classname = m.__class__.__name__
    if classname.find('BatchNorm') != -1 or classname.find('InPlaceABN') != -1:
        m.momentum = 0.0003

        
def build_transforms(args, is_train=True):
    normalize = T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    
    h, w = map(int, args.input_size.split(','))
    input_size = [h, w]

    if 'train' in args.dataset:
        print('----build transform for training----')
        transform = T.Compose([
#             T.Resize(cfg.INPUT.SIZE_TRAIN, interpolation = 3),
            T.RandomHorizontalFlip(p=0.5),
            T.RandomVerticalFlip(p=0.5),
            T.RandomCrop(input_size),
            T.ToTensor(),
            normalize
        ])
        
        mask_transform = T.Compose([
#             T.Resize(cfg.INPUT.SIZE_TRAIN, interpolation = 0),
            T.RandomHorizontalFlip(p=0.5),
            T.RandomVerticalFlip(p=0.5),
            T.RandomCrop(input_size),
            T.ToTensor()
        ])

    elif 'test' in args.dataset:
        print('----build transform for testing----')
        transform = T.Compose([
#             T.Resize(cfg.INPUT.SIZE_TEST),
            T.ToTensor(),
            normalize
        ])
        mask_transform = T.Compose([
#             T.Resize(cfg.INPUT.SIZE_TEST),
            T.ToTensor()
        ])
    elif 'test_no_label' in args.dataset:
        print('----build transform for test_no_label----')
        transform = T.Compose([
#             T.Resize(cfg.INPUT.SIZE_TEST),
            T.ToTensor(),
            normalize
        ])
        mask_transform = None
    return (transform, mask_transform)


def main():
    """Create the model and start the training."""
    print (args)
    if not os.path.exists(args.snapshot_dir):
        os.makedirs(args.snapshot_dir)

    writer = SummaryWriter(args.snapshot_dir)
    gpus = [int(i) for i in args.gpu.split(',')]
    if not args.gpu == 'None':
        os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu

    h, w = map(int, args.input_size.split(','))
    input_size = [h, w]

    # cudnn related setting
    cudnn.enabled = True
    cudnn.benchmark = True
    torch.backends.cudnn.deterministic = False
    torch.backends.cudnn.enabled = True
 

    deeplab = get_cls_net(config=config, num_classes=args.num_classes, is_train=True)
    
    print('-------Load Weight', args.restore_from)
    saved_state_dict = torch.load(args.restore_from)
	
    if args.start_epoch > 0:
        model = DataParallelModel(deeplab)
        model.load_state_dict(saved_state_dict['state_dict'])
    else:
        new_params = deeplab.state_dict().copy()
        state_dict_pretrain = saved_state_dict
        for state_name in state_dict_pretrain:
            if state_name in new_params:
                new_params[state_name] = state_dict_pretrain[state_name]
                # print ('LOAD',state_name)
            else:
                print ('NOT LOAD', state_name)
        deeplab.load_state_dict(new_params)
        model = DataParallelModel(deeplab)
    print('-------Load Weight Finish', args.restore_from)
    
    model.cuda()

    criterion = CriterionAll()
    criterion = DataParallelCriterion(criterion)
    criterion.cuda()


#     transform = T.Compose([
#         transforms.ToTensor(),
#         normalize,
#     ])
    transform = build_transforms(args)

    print("-------Loading data...")
    parsing_dataset = WYDataSet(args.data_dir, args.dataset, crop_size=input_size, transform=transform)
    print("Data dir : ", args.data_dir)
    print("Dataset : ", args.dataset, "Sample Number: ", parsing_dataset.number_samples)
    trainloader = data.DataLoader(parsing_dataset,
                                  batch_size=args.batch_size * len(gpus), shuffle=True, num_workers=8,
                                  pin_memory=True)

    optimizer = optim.SGD(
        model.parameters(),
        lr=args.learning_rate,
        momentum=args.momentum,
        weight_decay=args.weight_decay
    )
    
    if args.start_epoch > 0:
        optimizer.load_state_dict(saved_state_dict['optimizer'])
        print ('========Load Optimizer',args.restore_from)
    

    total_iters = args.epochs * len(trainloader)
    for epoch in range(args.start_epoch, args.epochs):
        model.train()
        for i_iter, batch in enumerate(trainloader):
            i_iter += len(trainloader) * epoch
            lr = adjust_learning_rate(optimizer, i_iter, total_iters)

            images, labels = batch
            labels = labels.squeeze(1)
            labels = labels.long().cuda(non_blocking=True)
            preds = model(images)

            loss = criterion(preds, labels)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            if i_iter % 100 == 0:
                writer.add_scalar('learning_rate', lr, i_iter)
                writer.add_scalar('loss', loss.data.cpu().numpy(), i_iter)

            print(f'epoch = {epoch}, iter = {i_iter}/{total_iters}, lr={lr:.6f}, loss = {loss.data.cpu().numpy():.6f}')
        
        if (epoch+1) % args.save_step == 0 or epoch == args.epochs:
            time.sleep(10)
            print("-------Saving checkpoint...")
            save_checkpoint(model, epoch, optimizer)

    time.sleep(10)
    save_checkpoint(model, epoch, optimizer)
    end = timeit.default_timer()
    print(end - start, 'seconds')


def save_checkpoint(model, epoch, optimizer):
    state = {
        'epoch': epoch,
        'state_dict': model.state_dict(),
        'optimizer': optimizer.state_dict(),
    }
    filepath = osp.join(args.snapshot_dir, 'hrnet_epoch_' + str(epoch+1) + '.pth')
    torch.save(state, filepath)


if __name__ == '__main__':
    main()
