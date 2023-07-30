from __future__ import print_function
import argparse, random, copy
from typing import Callable
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.modules.module import Module
import torch.optim as optim
import torchvision
from torchvision.io import ImageReadMode
import torchvision.transforms as transforms
from torch.utils.data import Dataset, DataLoader
from torch.optim import Optimizer
from torchvision import datasets
from torchvision import transforms as T
from torch.optim.lr_scheduler import StepLR

import glob
import os
from sklearn.model_selection import StratifiedShuffleSplit
from PIL import Image

class ResnetBackbone(nn.Module):
    def __init__(self) -> None:
        super(ResnetBackbone, self).__init__()
        self.bone = torchvision.models.resnet18(weights=None)
        
        # over-write the first conv layer to be able to read MNIST images
        # as resnet18 reads (3,x,x) where 3 is RGB channels
        # whereas MNIST has (1,x,x) where 1 is a gray-scale channel
        self.bone.conv1 = nn.Conv2d(1, 64, kernel_size=(7, 7), stride=(2, 2), padding=(3, 3), bias=False)
        self.fc_in_features = self.bone.fc.in_features
        
        # remove the last layer of resnet18 (linear layer which is before avgpool layer)
        self.bone = torch.nn.Sequential(*(list(self.bone.children())[:-1]))
        
    def forward(self, x):
        output = self.bone(x)
        output = output.view(output.size()[0], -1)
        return output

class SiameseNetwork(nn.Module):
    """
        Siamese network for image similarity estimation.
        The network is composed of two identical networks, one for each input.
        The output of each network is concatenated and passed to a linear layer. 
        The output of the linear layer passed through a sigmoid function.
        `"FaceNet" <https://arxiv.org/pdf/1503.03832.pdf>`_ is a variant of the Siamese network.
        This implementation varies from FaceNet as we use the `ResNet-18` model from
        `"Deep Residual Learning for Image Recognition" <https://arxiv.org/pdf/1512.03385.pdf>`_ as our feature extractor.
        In addition, we aren't using `TripletLoss` as the MNIST dataset is simple, so `BCELoss` can do the trick.
    """
    def __init__(self):
        super(SiameseNetwork, self).__init__()
        # get resnet model
        self.back = ResnetBackbone()
        self.fc_in_features = self.back.fc_in_features

        # add linear layers to compare between the features of the two images
        self.fc = nn.Sequential(
            nn.Linear(self.fc_in_features * 2, 256),
            nn.ReLU(inplace=True),
            nn.Linear(256, 1),
        )

        self.sigmoid = nn.Sigmoid()

        # initialize the weights
        self.back.bone.apply(self.init_weights)
        self.fc.apply(self.init_weights)
        
    def init_weights(self, m):
        if isinstance(m, nn.Linear):
            torch.nn.init.xavier_uniform_(m.weight)
            m.bias.data.fill_(0.01)

    def forward(self, input1, input2):
        # get two images' features
        output1 = self.back(input1)
        output2 = self.back(input2)

        # concatenate both images' features
        output = torch.cat((output1, output2), 1)

        # pass the concatenation to the linear layers
        output = self.fc(output)

        # pass the out of the linear layers to sigmoid layer
        output = self.sigmoid(output)
        
        return output

class IrisDataset(Dataset):
    def __init__(self, file_names: list[str], labels, final_height, final_width, 
                 transform: transforms.Compose = transforms.Compose([transforms.ToTensor()])):
        super(IrisDataset, self).__init__()
        # Transformações usadas no dataset
        size = len(file_names)
        self.images = torch.empty([size, 1, final_height, final_width], dtype=torch.float32)
        
        for file_name in file_names:
            img = Image.open(file_name)
            img: torch.Tensor = transform(img)
            self.images[0] = img

        self.labels = labels
        
        # TODO:
        # - Ordenar as imagens por label, aí cria um dict onde a chave é a label e o valor é 
        #   uma lista de índices do self.images, a fim de ser usado no __get_item__.
        
        # get MNIST dataset
        # self.dataset = datasets.MNIST(root, train=train, download=download)
        
        # as `self.dataset.data`'s shape is (Nx28x28), where N is the number of
        # examples in MNIST dataset, a single example has the dimensions of
        # (28x28) for (WxH), where W and H are the width and the height of the image. 
        # However, every example should have (CxWxH) dimensions where C is the number 
        # of channels to be passed to the network. As MNIST contains gray-scale images, 
        # we add an additional dimension to corresponds to the number of channels.
        
        # self.data = self.dataset.data.unsqueeze(1).clone()

        # self.group_examples()

    def group_examples(self):
        """
            To ease the accessibility of data based on the class, we will use `group_examples` to group 
            examples based on class. 
            
            Every key in `grouped_examples` corresponds to a class in MNIST dataset. For every key in 
            `grouped_examples`, every value will conform to all of the indices for the MNIST 
            dataset examples that correspond to that key.
        """

        # get the targets from MNIST dataset
        np_arr = np.array(self.dataset.targets.clone())
        
        # group examples based on class
        self.grouped_examples = {}
        for i in range(0,10):
            self.grouped_examples[i] = np.where((np_arr==i))[0]
    
    def __len__(self):
        return self.data.shape[0]
    
    def __getitem__(self, index):
        """
            For every example, we will select two images. There are two cases, 
            positive and negative examples. For positive examples, we will have two 
            images from the same class. For negative examples, we will have two images 
            from different classes.

            Given an index, if the index is even, we will pick the second image from the same class, 
            but it won't be the same image we chose for the first class. This is used to ensure the positive
            example isn't trivial as the network would easily distinguish the similarity between same images. However,
            if the network were given two different images from the same class, the network will need to learn 
            the similarity between two different images representing the same class. If the index is odd, we will 
            pick the second image from a different class than the first image.
        """

        # pick some random class for the first image
        selected_class = random.randint(0, 9)

        # pick a random index for the first image in the grouped indices based of the label
        # of the class
        random_index_1 = random.randint(0, self.grouped_examples[selected_class].shape[0]-1)
        
        # pick the index to get the first image
        index_1 = self.grouped_examples[selected_class][random_index_1]

        # get the first image
        image_1 = self.data[index_1].clone().float()

        # same class
        if index % 2 == 0:
            # pick a random index for the second image
            random_index_2 = random.randint(0, self.grouped_examples[selected_class].shape[0]-1)
            
            # ensure that the index of the second image isn't the same as the first image
            while random_index_2 == random_index_1:
                random_index_2 = random.randint(0, self.grouped_examples[selected_class].shape[0]-1)
            
            # pick the index to get the second image
            index_2 = self.grouped_examples[selected_class][random_index_2]

            # get the second image
            image_2 = self.data[index_2].clone().float()

            # set the label for this example to be positive (1)
            target = torch.tensor(1, dtype=torch.float)
        
        # different class
        else:
            # pick a random class
            other_selected_class = random.randint(0, 9)

            # ensure that the class of the second image isn't the same as the first image
            while other_selected_class == selected_class:
                other_selected_class = random.randint(0, 9)

            
            # pick a random index for the second image in the grouped indices based of the label
            # of the class
            random_index_2 = random.randint(0, self.grouped_examples[other_selected_class].shape[0]-1)

            # pick the index to get the second image
            index_2 = self.grouped_examples[other_selected_class][random_index_2]

            # get the second image
            image_2 = self.data[index_2].clone().float()

            # set the label for this example to be negative (0)
            target = torch.tensor(0, dtype=torch.float)

        return image_1, image_2, target


def train_loop(train_loader: DataLoader,
               model: nn.Module,
               loss_fn: nn.Module,
               optimizer: Optimizer,
               device: torch.device,
               epochs: int,
               log_interval: int,
               dry_run: bool,
               **kwargs):
    model.train()

    # we aren't using `TripletLoss` as the MNIST dataset is simple, so `BCELoss` can do the trick.

    for batch_idx, (images_1, images_2, targets) in enumerate(train_loader):
        images_1, images_2, targets = images_1.to(device), images_2.to(device), targets.to(device)
        optimizer.zero_grad()
        outputs = model(images_1, images_2).squeeze()
        loss = loss_fn(outputs, targets)
        loss.backward()
        optimizer.step()
        if batch_idx % log_interval == 0:
            print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                epochs, batch_idx * len(images_1), len(train_loader.dataset),
                100. * batch_idx / len(train_loader), loss.item()))
            if dry_run:
                break


def test_loop(test_loader: DataLoader, model: nn.Module, loss_fn: nn.Module, device: torch.device):
    model.eval()
    test_loss = 0
    correct = 0

    # we aren't using `TripletLoss` as the MNIST dataset is simple, so `BCELoss` can do the trick.

    with torch.no_grad():
        for (images_1, images_2, targets) in test_loader:
            images_1, images_2, targets = images_1.to(device), images_2.to(device), targets.to(device)
            outputs = model(images_1, images_2).squeeze()
            test_loss += loss_fn(outputs, targets).sum().item()  # sum up batch loss
            pred = torch.where(outputs > 0.5, 1, 0)  # get the index of the max log-probability
            correct += pred.eq(targets.view_as(pred)).sum().item()

    test_loss /= len(test_loader.dataset)

    # for the 1st epoch, the average loss is 0.0001 and the accuracy 97-98%
    # using default settings. After completing the 10th epoch, the average
    # loss is 0.0000 and the accuracy 99.5-100% using default settings.
    print('\nTest set: Average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n'.format(
        test_loss, correct, len(test_loader.dataset),
        100. * correct / len(test_loader.dataset)))


def main():
    # Training settings
    parser = argparse.ArgumentParser(description='PyTorch Siamese network Example')
    parser.add_argument('--batch-size', type=int, default=64, metavar='N',
                        help='input batch size for training (default: 64)')
    parser.add_argument('--test-batch-size', type=int, default=1000, metavar='N',
                        help='input batch size for testing (default: 1000)')
    parser.add_argument('--epochs', type=int, default=14, metavar='N',
                        help='number of epochs to train (default: 14)')
    parser.add_argument('--lr', type=float, default=1.0, metavar='LR',
                        help='learning rate (default: 1.0)')
    parser.add_argument('--gamma', type=float, default=0.7, metavar='M',
                        help='Learning rate step gamma (default: 0.7)')
    parser.add_argument('--no-cuda', action='store_true', default=False,
                        help='disables CUDA training')
    parser.add_argument('--no-mps', action='store_true', default=False,
                        help='disables macOS GPU training')
    parser.add_argument('--dry-run', action='store_true', default=False,
                        help='quickly check a single pass')
    parser.add_argument('--seed', type=int, default=42, metavar='S',
                        help=f'random seed (default: 42)')
    parser.add_argument('--log-interval', type=int, default=10, metavar='N',
                        help='how many batches to wait before logging training status')
    parser.add_argument('--save-model', action='store_true', default=False,
                        help='For Saving the current Model')
    args = parser.parse_args()
    
    use_cuda = not args.no_cuda and torch.cuda.is_available()
    use_mps = not args.no_mps and torch.backends.mps.is_available()

    torch.manual_seed(args.seed)

    if use_cuda:
        device = torch.device("cuda")
    elif use_mps:
        device = torch.device("mps")
    else:
        device = torch.device("cpu")

    train_kwargs = {'batch_size': args.batch_size}
    test_kwargs = {'batch_size': args.test_batch_size}
    if use_cuda:
        cuda_kwargs = {'num_workers': 1,
                       'pin_memory': True,
                       'shuffle': True}
        train_kwargs.update(cuda_kwargs)
        test_kwargs.update(cuda_kwargs)

    DATASET_DIR = 'MMU-Iris-Database'
    class_names = np.sort(np.array(next(os.walk(DATASET_DIR))[1], np.float32))
    file_paths = np.sort(glob.glob(DATASET_DIR + '/*/*.bmp'))

    labels = np.array([path.split('/')[1] for path in file_paths], np.float32)
    labels -= 1
    
    splitter = StratifiedShuffleSplit(n_splits=1, train_size=0.6, test_size=0.4, random_state=args.seed)
    train_indices, test_indices = next(splitter.split(file_paths, labels))
    
    files_train, labels_train = file_paths[train_indices], labels[train_indices]
    files_test, labels_test = file_paths[test_indices], labels[test_indices]

    num_classes = np.unique(labels_train).shape[0]+1
    
    labels_train = torch.from_numpy(labels_train)
    labels_test = torch.from_numpy(labels_test)    
    
    FINAL_HEIGHT = 128
    FINAL_WIDTH = 128
    ds_transforms = transforms.Compose([transforms.Grayscale(num_output_channels=1), 
                                        transforms.Resize((FINAL_HEIGHT, FINAL_WIDTH)),
                                        transforms.ToTensor()])
    train_dataset = IrisDataset(files_train, labels_train, FINAL_HEIGHT, FINAL_WIDTH, ds_transforms)
    # test_dataset = IrisDataset(files_train, labels_train)
    # train_loader = torch.utils.data.DataLoader(train_dataset,**train_kwargs)
    # test_loader = torch.utils.data.DataLoader(test_dataset, **test_kwargs)

    model = SiameseNetwork().to(device)
    optimizer = optim.Adadelta(model.parameters(), lr=args.lr)
    loss_fn = nn.BCELoss()

    scheduler = StepLR(optimizer, step_size=1, gamma=args.gamma)
    # for epoch in range(1, args.epochs + 1):
    #     train_loop(train_loader, model, loss_fn, optimizer, device, **vars(args))
    #     test_loop(test_loader, model, loss_fn, device)
    #     scheduler.step()

    if args.save_model:
        torch.save(model.state_dict(), "siamese_network.pt")


if __name__ == '__main__':
    main()
