from email.policy import default
import torch
torch.set_default_tensor_type(torch.cuda.FloatTensor)
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import numpy as np
import math
import gzip
import itertools
from operator import itemgetter
import random
import wandb

from sac.utils import soft_update, hard_update
from sac.model import QNetwork, QNetwork_for_Model

device = torch.device('cuda')

num_train = 60000  # 60k train examples
num_test = 10000  # 10k test examples
train_inputs_file_path = './MNIST_data/train-images-idx3-ubyte.gz'
train_labels_file_path = './MNIST_data/train-labels-idx1-ubyte.gz'
test_inputs_file_path = './MNIST_data/t10k-images-idx3-ubyte.gz'
test_labels_file_path = './MNIST_data/t10k-labels-idx1-ubyte.gz'

BATCH_SIZE = 100


class StandardScaler(object):
    def __init__(self):
        pass

    def fit(self, data):
        """Runs two ops, one for assigning the mean of the data to the internal mean, and
        another for assigning the standard deviation of the data to the internal standard deviation.
        This function must be called within a 'with <session>.as_default()' block.

        Arguments:
        data (np.ndarray): A numpy array containing the input

        Returns: None.
        """
        self.mu = np.mean(data, axis=0, keepdims=True)
        self.std = np.std(data, axis=0, keepdims=True)
        self.std[self.std < 1e-12] = 1.0

    def transform(self, data):
        """Transforms the input matrix data using the parameters of this scaler.

        Arguments:
        data (np.array): A numpy array containing the points to be transformed.

        Returns: (np.array) The transformed dataset.
        """
        return (data - self.mu) / self.std

    def inverse_transform(self, data):
        """Undoes the transformation performed by this scaler.

        Arguments:
        data (np.array): A numpy array containing the points to be transformed.

        Returns: (np.array) The transformed dataset.
        """
        return self.std * data + self.mu


def init_weights(m):
    def truncated_normal_init(t, mean=0.0, std=0.01):
        torch.nn.init.normal_(t, mean=mean, std=std)
        while True:
            cond = torch.logical_or(t < mean - 2 * std, t > mean + 2 * std)
            if not torch.sum(cond):
                break
            t = torch.where(cond, torch.nn.init.normal_(torch.ones(t.shape), mean=mean, std=std), t)
        return t

    if type(m) == nn.Linear or isinstance(m, EnsembleFC):
        input_dim = m.in_features
        truncated_normal_init(m.weight, std=1 / (2 * np.sqrt(input_dim)))
        m.bias.data.fill_(0.0)


class EnsembleFC(nn.Module):
    __constants__ = ['in_features', 'out_features']
    in_features: int
    out_features: int
    ensemble_size: int
    weight: torch.Tensor

    def __init__(self, in_features: int, out_features: int, ensemble_size: int, weight_decay: float = 0., bias: bool = True) -> None:
        super(EnsembleFC, self).__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.ensemble_size = ensemble_size
        self.weight = nn.Parameter(torch.Tensor(ensemble_size, in_features, out_features))
        self.weight_decay = weight_decay
        if bias:
            self.bias = nn.Parameter(torch.Tensor(ensemble_size, out_features))
        else:
            self.register_parameter('bias', None)
        self.reset_parameters()

    def reset_parameters(self) -> None:
        pass

    def forward(self, input: torch.Tensor) -> torch.Tensor:
        w_times_x = torch.bmm(input, self.weight)
        return torch.add(w_times_x, self.bias[:, None, :])  # w times x + b

    def extra_repr(self) -> str:
        return 'in_features={}, out_features={}, bias={}'.format(self.in_features, self.out_features, self.bias is not None)

def xavier_weights_init_(m):
    def truncated_xavier_init(t, mean=0.0, std=0.01):
        torch.nn.init.normal_(t, mean=mean, std=std)
        while True:
            cond = torch.logical_or(t < mean - 2 * std, t > mean + 2 * std)
            if not torch.sum(cond):
                break
            t = torch.where(cond, torch.nn.init.xavier_uniform_(m.weight, gain=1))
        return t
    if type(m) == nn.Linear or isinstance(m, EnsembleFC):
        input_dim = m.in_features
        truncated_xavier_init(m.weight, std=1 / (2 * np.sqrt(input_dim)))
        m.bias.data.fill_(0.0)
        torch.nn.init.constant_(m.bias, 0)

class EnsembleModel(nn.Module):
    def __init__(self, args, state_size, action_size, reward_size, ensemble_size, hidden_size=200, learning_rate=1e-3, use_decay=False):
        super(EnsembleModel, self).__init__()
        self.args = args
        self.hidden_size = hidden_size
        self.nn1 = EnsembleFC(state_size + action_size, hidden_size, ensemble_size, weight_decay=0.000025)
        self.nn2 = EnsembleFC(hidden_size, hidden_size, ensemble_size, weight_decay=0.00005)
        self.nn3 = EnsembleFC(hidden_size, hidden_size, ensemble_size, weight_decay=0.000075)
        self.nn4 = EnsembleFC(hidden_size, hidden_size, ensemble_size, weight_decay=0.000075)
        self.use_decay = use_decay
        self.swish = Swish()

        self.output_dim = state_size + reward_size
        # Add variance output
        out_dim = self.output_dim if args.deter_model else self.output_dim * 2
        self.nn5 = EnsembleFC(hidden_size, out_dim, ensemble_size, weight_decay=0.0001)

        self.max_logvar = nn.Parameter((torch.ones((1, self.output_dim)).float() / 2).to(device), requires_grad=False)
        self.min_logvar = nn.Parameter((-torch.ones((1, self.output_dim)).float() * 10).to(device), requires_grad=False)
        self.optimizer = torch.optim.Adam(self.parameters(), lr=learning_rate)
        self.apply(init_weights)

    def forward(self, x, ret_log_var=False):
        nn1_output = self.swish(self.nn1(x))
        nn2_output = self.swish(self.nn2(nn1_output))
        nn3_output = self.swish(self.nn3(nn2_output))
        nn4_output = self.swish(self.nn4(nn3_output))
        nn5_output = self.nn5(nn4_output)

        mean = nn5_output[:, :, :self.output_dim]
        if self.args.deter_model:
            return mean

        logvar = self.max_logvar - F.softplus(self.max_logvar - nn5_output[:, :, self.output_dim:])
        logvar = self.min_logvar + F.softplus(logvar - self.min_logvar)

        if ret_log_var:
            return mean, logvar
        else:
            return mean, torch.exp(logvar)

    def get_decay_loss(self):
        decay_loss = 0.
        for m in self.children():
            if isinstance(m, EnsembleFC):
                decay_loss += m.weight_decay * torch.sum(torch.square(m.weight)) / 2.
                # print(m.weight.shape)
                # print(m, decay_loss, m.weight_decay)
        return decay_loss

    def deter_loss(self, mean, labels):
        """
        mean, logvar: Ensemble_size x N x dim
        labels: N x dim
        """
        mse_loss = torch.mean(torch.pow(mean - labels, 2), dim=(1, 2))
        total_loss = torch.mean(mse_loss)
        return total_loss, mse_loss

    def loss(self, mean, logvar, labels, inc_var_loss=True):
        """
        mean, logvar: Ensemble_size x N x dim
        labels: N x dim
        """
        assert len(mean.shape) == len(logvar.shape) == len(labels.shape) == 3
        inv_var = torch.exp(-logvar)
        if inc_var_loss:
            # Average over batch and dim, sum over ensembles.
            mse_loss = torch.mean(torch.mean(torch.pow(mean - labels, 2) * inv_var, dim=-1), dim=-1)
            var_loss = torch.mean(torch.mean(logvar, dim=-1), dim=-1)
            total_loss = torch.mean(mse_loss) + torch.mean(var_loss)
        else:
            mse_loss = torch.mean(torch.pow(mean - labels, 2), dim=(1, 2))
            total_loss = torch.mean(mse_loss)
        return total_loss, mse_loss

    def train(self, loss):
        self.optimizer.zero_grad()

        loss += 0.01 * torch.sum(self.max_logvar) - 0.01 * torch.sum(self.min_logvar)
        if self.use_decay:
            loss += self.get_decay_loss()
        loss.backward()
        self.optimizer.step()


class EnsembleDynamicsModel():
    def __init__(self, args, network_size, elite_size, state_size, action_size, reward_size=1, hidden_size=200, use_decay=False):
        self.args = args
        self.RL_b_size = args.RLmodel_batch_size
        self.network_size = network_size
        self.elite_size = elite_size
        self.model_list = []
        self.state_size = state_size
        self.action_size = action_size
        self.reward_size = reward_size
        self.elite_model_idxes = random.sample([i for i in range(network_size)], args.num_elites)
        self.ensemble_model = EnsembleModel(args, state_size, action_size, reward_size, network_size, hidden_size, use_decay=use_decay)
        self.scaler = StandardScaler()
        self.m_train_count = 0
        if args.RLembededtrain:
            self.critic = QNetwork(args.s_dim + args.a_dim, args.s_dim + 1, args.hidden_size).to(self.device)
            self.critic_target = QNetwork(args.s_dim + args.a_dim, args.s_dim + 1, args.hidden_size).to(self.device)
            self.critic_optim = torch.optim.Adam(self.critic.parameters(), lr=args.model_q_lr)
            hard_update(self.critic_target, self.critic)
            self.q_mean = None
            self.a_mean = None
            self.m_train_count = 0

    def train(self, inputs, labels, batch_size=256, holdout_ratio=0., max_epochs_since_update=5):
        # print(inputs.shape, labels.shape) # (B, s_dim+a_dim) (B, s_dim+1)
        self._max_epochs_since_update = max_epochs_since_update
        self._epochs_since_update = 0
        self._state = {}
        self._snapshots = {i: (None, 1e10) for i in range(self.network_size)}

        num_holdout = int(inputs.shape[0] * holdout_ratio)
        permutation = np.random.permutation(inputs.shape[0])
        inputs, labels = inputs[permutation], labels[permutation]
        # print(inputs.shape) (len(pool), _dim)

        train_inputs, train_labels = inputs[num_holdout:], labels[num_holdout:]
        holdout_inputs, holdout_labels = inputs[:num_holdout], labels[:num_holdout]

        self.scaler.fit(train_inputs)
        train_inputs = self.scaler.transform(train_inputs) # (len(env_pool), _dim)
        holdout_inputs = self.scaler.transform(holdout_inputs)

        holdout_inputs = torch.from_numpy(holdout_inputs).float().to(device)
        holdout_labels = torch.from_numpy(holdout_labels).float().to(device)
        holdout_inputs = holdout_inputs[None, :, :].repeat([self.network_size, 1, 1])
        holdout_labels = holdout_labels[None, :, :].repeat([self.network_size, 1, 1])
        for epoch in itertools.count():
            train_idx = np.vstack([np.random.permutation(train_inputs.shape[0]) for _ in range(self.network_size)])
            for start_pos in range(0, train_inputs.shape[0], batch_size):
                idx = train_idx[:, start_pos: start_pos + batch_size] # (n_ens, b_size)
                train_input = torch.from_numpy(train_inputs[idx]).float().to(device) # (n_ens, b_size, _dim)
                train_label = torch.from_numpy(train_labels[idx]).float().to(device)
                losses = []
                if self.args.deter_model:
                    mean = self.ensemble_model(train_input, ret_log_var=True)
                    loss, _ = self.ensemble_model.deter_loss(mean, train_label)
                else:
                    mean, logvar = self.ensemble_model(train_input, ret_log_var=True)
                    loss, _ = self.ensemble_model.loss(mean, logvar, train_label)
                self.ensemble_model.train(loss)
                losses.append(loss)

            with torch.no_grad():
                if self.args.deter_model:
                    holdout_mean = self.ensemble_model(holdout_inputs, ret_log_var=True)
                    _, holdout_mse_losses = self.ensemble_model.deter_loss(holdout_mean, holdout_labels)
                else:
                    holdout_mean, holdout_logvar = self.ensemble_model(holdout_inputs, ret_log_var=True)
                    _, holdout_mse_losses = self.ensemble_model.loss(holdout_mean, holdout_logvar, holdout_labels, inc_var_loss=False)
                holdout_mse_losses = holdout_mse_losses.detach().cpu().numpy() # (n_ens,)
                sorted_loss_idx = np.argsort(holdout_mse_losses)
                self.elite_model_idxes = sorted_loss_idx[:self.elite_size].tolist()
                break_train = self._save_best(epoch, holdout_mse_losses)
                if break_train:
                    break

    def _save_best(self, epoch, holdout_losses):
        updated = False
        for i in range(len(holdout_losses)):
            current = holdout_losses[i]
            _, best = self._snapshots[i]
            improvement = (best - current) / best
            if improvement > 0.01:
                self._snapshots[i] = (epoch, current)
                updated = True
        if updated:
            self._epochs_since_update = 0
        else:
            self._epochs_since_update += 1
        if self._epochs_since_update > self._max_epochs_since_update:
            return True
        else:
            return False

    def predict(self, inputs, batch_size=1024, factored=True):
        if self.args.train_model_by_RL:
            self.scaler.fit(inputs)
        inputs = self.scaler.transform(inputs)
        ensemble_mean, ensemble_var = [], []
        for i in range(0, inputs.shape[0], batch_size):
            input = torch.from_numpy(inputs[i:min(i + batch_size, inputs.shape[0])]).float().to(device)
            b_mean, b_var = self.ensemble_model(input[None, :, :].repeat([self.network_size, 1, 1]), ret_log_var=False)
            ensemble_mean.append(b_mean.detach().cpu().numpy())
            ensemble_var.append(b_var.detach().cpu().numpy())
        ensemble_mean = np.hstack(ensemble_mean)
        ensemble_var = np.hstack(ensemble_var)

        if factored:
            return ensemble_mean, ensemble_var
        else:
            assert False, "Need to transform to numpy"

    def deter_predict(self, inputs, batch_size=1024):
        if self.args.train_model_by_RL:
            self.scaler.fit(inputs)
        inputs = self.scaler.transform(inputs)
        ensemble_mean = []
        for i in range(0, inputs.shape[0], batch_size):
            input = torch.from_numpy(inputs[i:min(i + batch_size, inputs.shape[0])]).float().to(device)
            b_mean = self.ensemble_model(input[None, :, :].repeat([self.network_size, 1, 1]))
            ensemble_mean.append(b_mean.detach().cpu().numpy())
        ensemble_mean = np.hstack(ensemble_mean)

        return ensemble_mean

    def sample(self, inputs):
        # build N(en_mean, en_var)
        en_mean, en_var = self.ensemble_model(inputs.unsqueeze(0).repeat(self.network_size, 1, 1)) # shape(RLmodel_batch_size,)
        en_std = torch.sqrt(en_var)
        # en_mean, en_var = en_mean[:, :, 1:], en_var[:, :, 1:]
        # en_mean, en_std = en_mean[self.elite_model_idxes].mean(0), en_std[self.elite_model_idxes].mean(0) # TODO
        # en_mean, en_std = en_mean.mean(0), torch.sqrt(en_var).mean(0)
        action_scale = torch.ones_like(en_mean).cuda()
        action_bias = torch.zeros_like(en_std).cuda()

        unit_normal = torch.distributions.normal.Normal(action_bias, action_scale)
        normal = torch.distributions.normal.Normal(en_mean, en_std)
        eps = unit_normal.sample()
        action = en_mean + eps * en_std
        log_prob = normal.log_prob(action)
        return action, log_prob, en_mean, en_std

class Swish(nn.Module):
    def __init__(self):
        super(Swish, self).__init__()

    def forward(self, x): # NOTE
        x = x * F.sigmoid(x) # (n_ens, 1, hidden_size)
        return x


def get_data(inputs_file_path, labels_file_path, num_examples):
    with open(inputs_file_path, 'rb') as f, gzip.GzipFile(fileobj=f) as bytestream:
        bytestream.read(16)
        buf = bytestream.read(28 * 28 * num_examples)
        data = np.frombuffer(buf, dtype=np.uint8) / 255.0
        inputs = data.reshape(num_examples, 784)

    with open(labels_file_path, 'rb') as f, gzip.GzipFile(fileobj=f) as bytestream:
        bytestream.read(8)
        buf = bytestream.read(num_examples)
        labels = np.frombuffer(buf, dtype=np.uint8)

    return np.array(inputs, dtype=np.float32), np.array(labels, dtype=np.int8)


def set_tf_weights(model, tf_weights):
    print(tf_weights.keys())
    pth_weights = {}
    pth_weights['max_logvar'] = tf_weights['BNN/max_log_var:0']
    pth_weights['min_logvar'] = tf_weights['BNN/min_log_var:0']
    pth_weights['nn1.weight'] = tf_weights['BNN/Layer0/FC_weights:0']
    pth_weights['nn1.bias'] = tf_weights['BNN/Layer0/FC_biases:0']
    pth_weights['nn2.weight'] = tf_weights['BNN/Layer1/FC_weights:0']
    pth_weights['nn2.bias'] = tf_weights['BNN/Layer1/FC_biases:0']
    pth_weights['nn3.weight'] = tf_weights['BNN/Layer2/FC_weights:0']
    pth_weights['nn3.bias'] = tf_weights['BNN/Layer2/FC_biases:0']
    pth_weights['nn4.weight'] = tf_weights['BNN/Layer3/FC_weights:0']
    pth_weights['nn4.bias'] = tf_weights['BNN/Layer3/FC_biases:0']
    pth_weights['nn5.weight'] = tf_weights['BNN/Layer4/FC_weights:0']
    pth_weights['nn5.bias'] = tf_weights['BNN/Layer4/FC_biases:0']
    for name, param in model.ensemble_model.named_parameters():
        if param.requires_grad:
            # print(name)
            print(param.data.shape, pth_weights[name].shape)
            param.data = torch.FloatTensor(pth_weights[name]).to(device).reshape(param.data.shape)
            pth_weights[name] = param.data
            print(name)


def main():
    torch.set_printoptions(precision=7)
    import pickle
    # Import MNIST train and test examples into train_inputs, train_labels, test_inputs, test_labels
    # train_inputs, train_labels = get_data(train_inputs_file_path, train_labels_file_path, num_train)
    # test_inputs, test_labels = get_data(test_inputs_file_path, test_labels_file_path, num_test)

    num_networks = 7
    num_elites = 5
    state_size = 17
    action_size = 6
    reward_size = 1
    pred_hidden_size = 200
    model = EnsembleDynamicsModel(num_networks, num_elites, state_size, action_size, reward_size, pred_hidden_size)

    # load tf weights and set it to be the inital weights for pytorch model
    with open('tf_weights.pkl', 'rb') as f:
        tf_weights = pickle.load(f)
    # set_tf_weights(model, tf_weights)
    # x = model.model_list[0].named_parameters()
    # for name, param in model.model_list[0].named_parameters():
    #     if param.requires_grad:
    #         print(name, param.shape)
    # exit()
    BATCH_SIZE = 5250
    import time
    st_time = time.time()
    with open('test.npy', 'rb') as f:
        train_inputs = np.load(f)
        train_labels = np.load(f)
    for i in range(0, 1000, BATCH_SIZE):
        # train_inputs = np.random.random([BATCH_SIZE, state_size + action_size])
        # train_labels = np.random.random([BATCH_SIZE, state_size + 1])
        model.train(train_inputs, train_labels, holdout_ratio=0.2)
        # mean, var = model.predict(train_inputs[:100])
        # print(mean[0])
        # print(mean.mean().item())
        # print(var[0])
        # print(var.mean().item())
        # exit()
    print(time.time() - st_time)
    # for name, param in model.model_list[0].named_parameters():
    #     if param.requires_grad:
    #         print(name, param.shape,param)
    exit()
    # for i in range(0, 10000, BATCH_SIZE):
    #     model.train(Variable(torch.from_numpy(train_inputs[i:i + BATCH_SIZE])), Variable(torch.from_numpy(train_labels[i:i + BATCH_SIZE])))
    #
    # model.predict(Variable(torch.from_numpy(test_inputs[:1000])))


if __name__ == '__main__':
    main()
