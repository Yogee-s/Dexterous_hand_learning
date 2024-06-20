import logging
logging.disable(logging.CRITICAL)
import numpy as np
import scipy as sp
import scipy.sparse.linalg as spLA
import copy
import time as timer
import torch
import torch.nn as nn
from torch.autograd import Variable
import copy

# samplers
# import mjrl.samplers.trajectory_sampler as trajectory_sampler
# import mjrl.samplers.batch_sampler as batch_sampler

# utility functions
import mjrl.utils.process_samples as process_samples
from mjrl.utils.logger import DataLog
from mjrl.utils.cg_solve import cg_solve

# Import Algs
from mjrl.algos.npg_cg import NPG
# from mjrl.algos.behavior_cloning import BC

from tpi.core.config import cfg


class DAPG(NPG):
    def __init__(self, env, policy, baseline,
                 demo_paths=None,
                 normalized_step_size=0.01,
                 FIM_invert_args={'iters': 10, 'damping': 1e-4},
                 hvp_sample_frac=1.0,
                 seed=None,
                 save_logs=False,
                 kl_dist=None,
                 lam_0=1.0,  # demo coef
                 lam_1=0.95, # decay coef
                 ):

        self.env = env
        self.policy = policy
        self.baseline = baseline
        self.kl_dist = kl_dist if kl_dist is not None else 0.5*normalized_step_size
        self.seed = seed
        self.save_logs = save_logs
        self.FIM_invert_args = FIM_invert_args
        self.hvp_subsample = hvp_sample_frac
        self.running_score = None
        self.demo_paths = demo_paths
        self.lam_0 = lam_0
        self.lam_1 = lam_1
        self.iter_count = 0.0
        if save_logs: self.logger = DataLog()

    def train_from_paths(self, paths):
        ##############################################################
        ##############################################################
        ##############################################################
        obs_indexes = [0, 1, 2, 3, 4, 5, 9, 10, 13, 14, 17, 18, 22, 23, 25, 26, 28, 29,30,31,32,33,34,35,36,37,38]
        act_indexes = [0, 1, 2, 3, 4, 5, 9, 10, 13, 14, 17, 18, 22, 23, 25, 26, 28, 29]
        # Concatenate from all the trajectories
        observations = np.concatenate([path["observations"] for path in paths])
        actions = np.concatenate([path["actions"] for path in paths])
        advantages = np.concatenate([path["advantages"] for path in paths])
        advantages = (advantages - np.mean(advantages)) / (np.std(advantages) + 1e-6)

        if self.demo_paths is not None and self.lam_0 > 0.0:
            demo_obs = np.concatenate([path["observations"][:, obs_indexes] for path in self.demo_paths])
            demo_act = np.concatenate([path["actions"][:, act_indexes] for path in self.demo_paths])
            demo_adv = self.lam_0 * (self.lam_1 ** self.iter_count) * np.ones(demo_obs.shape[0])
            self.iter_count += 1
            # concatenate all
            all_obs = np.concatenate([observations, demo_obs])
            all_act = np.concatenate([actions, demo_act])
            all_adv = cfg.DAPG_ADV_W * np.concatenate([advantages/(np.std(advantages) + 1e-8), demo_adv])
        else:
            # all_obs = observations[:, obs_indexes]
            # all_act = actions[:, act_indexes]
            # all_adv = advantages[:, act_indexes]
            all_obs = observations
            all_act = actions
            all_adv = advantages
        ##############################################################
        ##############################################################
        ##############################################################

        # # Concatenate from all the trajectories
        # observations = np.concatenate([path["observations"] for path in paths])
        # actions = np.concatenate([path["actions"] for path in paths])
        # advantages = np.concatenate([path["advantages"] for path in paths])
        # advantages = (advantages - np.mean(advantages)) / (np.std(advantages) + 1e-6)

        # if self.demo_paths is not None and self.lam_0 > 0.0:
        #     demo_obs = np.concatenate([path["observations"] for path in self.demo_paths])
        #     demo_act = np.concatenate([path["actions"] for path in self.demo_paths])
        #     demo_adv = self.lam_0 * (self.lam_1 ** self.iter_count) * np.ones(demo_obs.shape[0])
        #     self.iter_count += 1
        #     # concatenate all
        #     all_obs = np.concatenate([observations, demo_obs])
        #     all_act = np.concatenate([actions, demo_act])
        #     all_adv = cfg.DAPG_ADV_W * np.concatenate([advantages/(np.std(advantages) + 1e-8), demo_adv])
        # else:
        #     all_obs = observations
        #     all_act = actions
        #     all_adv = advantages

        # cache return distributions for the paths
        path_returns = [sum(p["rewards"]) for p in paths]
        mean_return = np.mean(path_returns)
        std_return = np.std(path_returns)
        min_return = np.amin(path_returns)
        max_return = np.amax(path_returns)
        base_stats = [mean_return, std_return, min_return, max_return]
        self.running_score = mean_return if self.running_score is None else \
                             0.9*self.running_score + 0.1*mean_return  # approx avg of last 10 iters
        if self.save_logs: self.log_rollout_statistics(paths)

        # Keep track of times for various computations
        t_gLL = 0.0
        t_FIM = 0.0

        # Optimization algorithm
        # --------------------------
        surr_before = self.CPI_surrogate(observations, actions, advantages).data.numpy().ravel()[0]

        # DAPG
        ts = timer.time()
        sample_coef = all_adv.shape[0]/advantages.shape[0]
        dapg_grad = sample_coef*self.flat_vpg(all_obs, all_act, all_adv)
        t_gLL += timer.time() - ts

        # NPG
        ts = timer.time()
        hvp = self.build_Hvp_eval([observations, actions],
                                  regu_coef=self.FIM_invert_args['damping'])
        npg_grad = cg_solve(hvp, dapg_grad, x_0=dapg_grad.copy(),
                            cg_iters=self.FIM_invert_args['iters'])
        t_FIM += timer.time() - ts

        # Step size computation
        # --------------------------
        n_step_size = 2.0*self.kl_dist
        alpha = np.sqrt(np.abs(n_step_size / (np.dot(dapg_grad.T, npg_grad) + 1e-20)))

        # Policy update
        # --------------------------
        #curr_params = self.learning.get_param_values()


        #print((npg_grad*hvp(npg_grad)))
        #print(type(npg_grad), type(hvp(npg_grad)))
        shs = 0.5 * (npg_grad * hvp(npg_grad)).sum(0, keepdims=True)
        lm = np.sqrt(shs / 1e-2)
        full_step = npg_grad / lm[0]
        grads = torch.autograd.grad(self.CPI_surrogate(all_obs, all_act, all_adv), self.policy.trainable_params)
        loss_grad = torch.cat([grad.view(-1)for grad in grads]).detach().numpy()
        print(loss_grad.shape, npg_grad.shape)
        neggdotstepdir = (loss_grad * npg_grad).sum(0, keepdims=True)
        print(f'dot value: {neggdotstepdir}')
        print('update by trpo')
        curr_params = self.policy.get_param_values()
        alpha = 1 # new implementation
        for k in range(10):
            new_params = curr_params + alpha * full_step
            self.policy.set_param_values(new_params, set_new=True, set_old=False)
            surr_after = self.CPI_surrogate(observations, actions, advantages).data.numpy().ravel()[0]
            kl_dist = self.kl_old_new(observations, actions).data.numpy().ravel()[0]
            
            actual_improve = (surr_after - surr_before)
            expected_improve = neggdotstepdir / lm[0] * alpha
            ratio = actual_improve / expected_improve
            print(f'ratio: {ratio}, lm: {lm}')
            
            if ratio.item() > .1 and actual_improve > 0:
                break
            else:
                alpha = 0.5 * alpha
                print('step size too high. backtracking. | kl = %f | suff diff = %f' % \
                        (kl_dist, surr_after-surr_before))
        
            if k == 9:
                alpha = 0

        new_params = curr_params + alpha * full_step
        self.policy.set_param_values(new_params, set_new=True, set_old=False)
        surr_after = self.CPI_surrogate(observations, actions, advantages).data.numpy().ravel()[0]
        kl_dist = self.kl_old_new(observations, actions).data.numpy().ravel()[0]
        self.policy.set_param_values(new_params, set_new=True, set_old=True)

        # Log information
        if self.save_logs:
            self.logger.log_kv('alpha', alpha)
            self.logger.log_kv('delta', n_step_size)
            self.logger.log_kv('time_vpg', t_gLL)
            self.logger.log_kv('time_npg', t_FIM)
            self.logger.log_kv('kl_dist', kl_dist)
            self.logger.log_kv('surr_improvement', surr_after - surr_before)
            self.logger.log_kv('running_score', self.running_score)
            try:
                self.env.env.env.evaluate_success(paths, self.logger)
            except:
                # nested logic for backwards compatibility. TODO: clean this up.
                try:
                    success_rate = self.env.env.env.evaluate_success(paths)
                    self.logger.log_kv('success_rate', success_rate)
                except:
                    pass
        return base_stats
