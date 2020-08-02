import argparse
import numpy as np
import tensorflow as tf
import time
import pickle

# add for encirclement
import gym
import os
import pylab as pyl
import re

import maddpg.common.tf_util as U
from maddpg.trainer.maddpg import MADDPGAgentTrainer
import tensorflow.contrib.layers as layers

on_train = True
tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.ERROR)

def parse_args():
    parser = argparse.ArgumentParser("Reinforcement Learning experiments for multiagent environments")
    # Environment
    parser.add_argument("--scenario", type=str, default="simple_encirclement", help="name of the scenario script")
    parser.add_argument("--max-episode-len", type=int, default=60, help="maximum episode length")
    parser.add_argument("--num-episodes", type=int, default=100000, help="number of episodes")
    parser.add_argument("--num-adversaries", type=int, default=0, help="number of adversaries")
    parser.add_argument("--good-policy", type=str, default="maddpg", help="policy for good agents")
    parser.add_argument("--adv-policy", type=str, default="maddpg", help="policy of adversaries")
    # Core training parameters
    parser.add_argument("--lr", type=float, default=3e-3, help="learning rate for Adam optimizer")
    parser.add_argument("--gamma", type=float, default=0.95, help="discount factor")
    parser.add_argument("--batch-size", type=int, default=1024, help="number of episodes to optimize at the same time")
    parser.add_argument("--num-units", type=int, default=64, help="number of units in the mlp")
    # Checkpointing
    parser.add_argument("--exp-name", type=str, default="encircle", help="name of the experiment")
    parser.add_argument("--save-dir", type=str, default="./policy/", help="directory in which training state and model should be saved")
    parser.add_argument("--save-rate", type=int, default=1000, help="save model once every time this many episodes are completed")
    parser.add_argument("--load-dir", type=str, default="", help="directory in which training state and model are loaded")
    # Evaluation
    parser.add_argument("--restore", action="store_true", default=False)
    parser.add_argument("--display", action="store_true", default=False)
    parser.add_argument("--benchmark", action="store_true", default=False)
    parser.add_argument("--benchmark-iters", type=int, default=100000, help="number of iterations run for benchmarking")
    parser.add_argument("--benchmark-dir", type=str, default="./benchmark_files/", help="directory where benchmark data is saved")
    parser.add_argument("--plots-dir", type=str, default="./learning_curves/", help="directory where plot data is saved")
    parser.add_argument("--data-dir", type=str, default="./data/", help="directory where error & reward data are saved")
    return parser.parse_args()

def mlp_model(input, num_outputs, scope, reuse=False, num_units=64, rnn_cell=None):
    # This model takes as input an observation and returns values of all actions
    with tf.variable_scope(scope, reuse=reuse):
        out = input
        out = layers.fully_connected(out, num_outputs=num_units, activation_fn=tf.nn.relu)
        out = layers.fully_connected(out, num_outputs=num_units, activation_fn=tf.nn.relu)
        out = layers.fully_connected(out, num_outputs=num_outputs, activation_fn=None)
        return out

def make_env(scenario_name, arglist, benchmark=False):
    from multiagent.environment import MultiAgentEnv
    import multiagent.scenarios as scenarios

    # load scenario from script
    scenario = scenarios.load(scenario_name + ".py").Scenario()
    # create world
    world = scenario.make_world()
    # create multiagent environment
    if benchmark:
        env = MultiAgentEnv(world, scenario.reset_world, scenario.reward, scenario.observation, scenario.geError_all, scenario.benchmark_data)
    else:
        env = MultiAgentEnv(world, scenario.reset_world, scenario.reward, scenario.observation, scenario.geError_all)
    return env

def get_trainers(env, num_adversaries, obs_shape_n, arglist):
    trainers = []
    model = mlp_model
    trainer = MADDPGAgentTrainer
    for i in range(num_adversaries):
        trainers.append(trainer(
            "agent_%d" % i, model, obs_shape_n, env.action_space, i, arglist,
            local_q_func=(arglist.adv_policy=='ddpg')))
    for i in range(num_adversaries, env.n):
        trainers.append(trainer(
            "agent_%d" % i, model, obs_shape_n, env.action_space, i, arglist,
            local_q_func=(arglist.good_policy=='ddpg')))
    return trainers  

def train(arglist):
    with U.single_threaded_session() as sess:
        # Create environment
        env = make_env(arglist.scenario, arglist, arglist.benchmark)
        # Create agent trainers
        obs_shape_n = [env.observation_space[i].shape for i in range(env.n)]
        num_adversaries = min(env.n, arglist.num_adversaries)
        trainers = get_trainers(env, num_adversaries, obs_shape_n, arglist)
        print('Using good policy {} and adv policy {}'.format(arglist.good_policy, arglist.adv_policy))

        # Initialize
        U.initialize()

        last_episode = [0.]
        all_episodes_before = 1000 #add for draw the tensorboard, default=1000
        
        # Load previous results, if necessary
        if arglist.load_dir == "":
            arglist.load_dir = arglist.save_dir + arglist.exp_name + '/'
        if arglist.display or arglist.restore or arglist.benchmark:
            print('Loading previous state...')
            U.load_state(arglist.load_dir)
        if arglist.restore:
            last_episode = re.findall("\d+",tf.train.latest_checkpoint(arglist.load_dir))
            print(str(last_episode[0]))


        # for load previous data to plot
        if arglist.restore and on_train is False:
            print('Reading previous learning data')
            rew_file_name = arglist.plots_dir + arglist.exp_name + '_rewards.pkl'
            with open(rew_file_name, 'rb') as fp:
                final_ep_rewards = pickle.load(fp)
            error_file_name = arglist.plots_dir + arglist.exp_name + '_error.pkl'
            with open(error_file_name,'rb') as fp:
                final_error = pickle.load(fp)
        else:
            final_ep_rewards = []  # sum of rewards for training curve
            final_error = []  # mean agents error for each control references

        episode_rewards = [0.0]  # sum of rewards for all agents
        agent_rewards = [[0.0] for _ in range(env.n)]  # individual agent reward
        final_ep_rewards = []  # sum of rewards for training curve
        final_ep_ag_rewards = []  # agent rewards for training curve
        agent_info = [[[]]]  # placeholder for benchmarking info
        saver = tf.train.Saver()
        obs_n = env.reset()
        episode_step = 0
        train_step = 0
        t_start = time.time()

        # add for error data
        episode_error_sum = np.zeros([env.n,4])
        episode_error_single = np.zeros([env.n,4])
        episode_reward_single = np.zeros([1,env.n])
        running_time = 0

        # add for tensorboard in train mode
        if not arglist.display:
            tf_reward = [0.] * env.n
            for i in range(env.n):
                tf_reward[i] = tf.placeholder(tf.float32)
                tf.summary.scalar('agent%i/mean_episode_rewards' %i, tf_reward[i])
            merged = tf.summary.merge_all() 
            writer = tf.summary.FileWriter("logs/", sess.graph)

        print('Starting iterations...')
        while True:
            # get action
            action_n = [agent.action(obs) for agent, obs in zip(trainers,obs_n)]
            # environment step
            new_obs_n, rew_n, done_n, info_n, error_n, reward_single = env.step(action_n)
            episode_step += 1
            # error data
            episode_error_sum = episode_error_sum + error_n
            episode_error_single += error_n
            episode_reward_single += reward_single

            done = all(done_n)
            terminal = (episode_step >= arglist.max_episode_len)
            # collect experience
            for i, agent in enumerate(trainers):
                agent.experience(obs_n[i], action_n[i], rew_n[i], new_obs_n[i], done_n[i], terminal)
            obs_n = new_obs_n

            for i, rew in enumerate(rew_n):
                episode_rewards[-1] += rew
                agent_rewards[i][-1] += rew

            if done or terminal:
                obs_n = env.reset()
                episode_step = 0
                episode_rewards.append(0)
                for a in agent_rewards:
                    a.append(0)
                agent_info.append([[]])

                # record error data
                if not arglist.display:
                    # print("episode error:",episode_error_single/arglist.max_episode_len)
                    # print(episode_error_single.shape[0])
                    file_name1 = ['0'] * env.n
                    file_name2 = arglist.data_dir + 'agent_reward_all.txt'
                    for i in range(0, episode_error_single.shape[0]):
                        file_name1[i] = arglist.data_dir + 'agent_error_' + str(i) + '.txt'
                    for i in range(0, episode_error_single.shape[0]):
                        error_str = ''
                        for j in range(0, episode_error_single.shape[1]):
                            error_str += str(np.around(episode_error_single[i][j]/arglist.max_episode_len,4)) + '    ' 
                        error_str += '\n'
                        with open(file_name1[i],'a') as f:
                            f.write(error_str)

                    reward_str = ''
                    for j in range(0,episode_reward_single.shape[1]):       
                        reward_str += str(np.around(episode_reward_single[0][j]/arglist.max_episode_len,4)) + '    '

                    reward_str += '\n'
                    with open(file_name2,'a') as f:
                        f.write(reward_str)

                    # add for tensorboard                                                 
                    rs = sess.run(merged, feed_dict={tf_reward[i] :\
                         np.around(episode_reward_single[0][i]/arglist.max_episode_len,4) for i in range(env.n)})
                    writer.add_summary(rs, len(episode_rewards)+int(last_episode[0])+all_episodes_before)

                    episode_error_single = np.zeros([env.n,4])
                    episode_reward_single = np.zeros([1,env.n])

            # increment global step counter
            train_step += 1

            # for benchmarking learned policies
            if arglist.benchmark:
                for i, info in enumerate(info_n):
                    agent_info[-1][i].append(info_n['n'])
                if train_step > arglist.benchmark_iters and (done or terminal):
                    file_name = arglist.benchmark_dir + arglist.exp_name + '.pkl'
                    print('Finished benchmarking, now saving...')
                    with open(file_name, 'wb') as fp:
                        pickle.dump(agent_info[:-1], fp)
                    break
                continue

            # for displaying learned policies
            if arglist.display:
                time.sleep(0.1)
                env.render()
                continue

            # update all trainers, if not in display or benchmark mode
            loss = None
            for agent in trainers:
                agent.preupdate()
            for agent in trainers:
                loss = agent.update(trainers, train_step)

            # save model, display training output
            if terminal and (len(episode_rewards) % arglist.save_rate == 0):
                U.save_state(arglist.save_dir + arglist.exp_name + '/', len(episode_rewards), saver=saver)
                # print statement depends on whether or not there are adversaries
                running_time += time.time()-t_start
                if num_adversaries == 0:
                    # print("steps: {}, episodes: {}, mean episode reward: {}, error: {}, episode time: {},  running time {}".format(
                    #     train_step, len(episode_rewards), np.round(np.mean(episode_rewards[-arglist.save_rate:]),2), \
                    #     np.round(episode_error_sum/(arglist.save_rate*arglist.max_episode_len),2), round(time.time()-t_start, 2), round(running_time,2)))
                    print("steps: {}, episodes: {}, mean episode reward: {}, episode time: {},  running time {}".format(
                        train_step, len(episode_rewards), np.round(np.mean(episode_rewards[-arglist.save_rate:]),2), \
                        round(time.time()-t_start, 2), round(running_time,2)))
                else:
                    print("steps: {}, episodes: {}, mean episode reward: {}, agent episode reward: {}, time: {}".format(
                        train_step, len(episode_rewards), np.mean(episode_rewards[-arglist.save_rate:]),
                        [np.mean(rew[-arglist.save_rate:]) for rew in agent_rewards], round(time.time()-t_start, 3)))
                t_start = time.time()
                # Keep track of final episode reward
                final_ep_rewards.append(np.mean(episode_rewards[-arglist.save_rate:]))
                for rew in agent_rewards:
                    final_ep_ag_rewards.append(np.mean(rew[-arglist.save_rate:]))
                # Keep track of final error
                final_error.append(np.around(episode_error_sum,2)/(arglist.save_rate*arglist.max_episode_len))
                episode_error_sum = np.zeros([1,4])

            # saves final episode reward for plotting training curve later
            if len(episode_rewards) > arglist.num_episodes:
                rew_file_name = arglist.plots_dir + arglist.exp_name + '_rewards.pkl'
                with open(rew_file_name, 'wb') as fp:
                    pickle.dump(final_ep_rewards, fp)
                agrew_file_name = arglist.plots_dir + arglist.exp_name + '_agrewards.pkl'
                with open(agrew_file_name, 'wb') as fp:
                    pickle.dump(final_ep_ag_rewards, fp)
                # save error data
                error_file_name = arglist.plots_dir + arglist.exp_name + '_error.pkl'
                with open(error_file_name,'wb') as fp:
                    pickle.dump(final_error,fp)      
                             
                print('...Finished total of {} episodes.'.format(len(episode_rewards)))
                break
# add for plot 
def curve_plot(arglist):
    rew_file_name = arglist.plots_dir + arglist.exp_name + '_rewards.pkl'
    with open(rew_file_name, 'rb') as fp:
        final_ep_rewards = pickle.load(fp)
    error_file_name = arglist.plots_dir + arglist.exp_name + '_error.pkl'
    with open(error_file_name,'rb') as fp:
        final_error = pickle.load(fp)

    print('plot learning curve for each episode...')
    t = np.linspace(0,len(final_ep_rewards)-1,len(final_ep_rewards))
    pyl.figure(1)
    pyl.plot(t,final_ep_rewards)
    pyl.xlabel('episodes')
    pyl.ylabel('episode rewards')
    pyl.figure(2)
    sub1 = pyl.subplot(221) # 在图表2中创建子图1
    sub2 = pyl.subplot(222) # 在图表2中创建子图2
    sub3 = pyl.subplot(223)
    sub4 = pyl.subplot(224)
    t = np.linspace(0,len(final_error)-1,len(final_error))
    pos_error = []
    phi_error = []
    w_error = []
    collision = []
    for i in range(len(final_error)):
        pos_error.append(final_error[i][0,0])
        phi_error.append(final_error[i][0,1])
        w_error.append(final_error[i][0,2])
        collision.append(final_error[i][0,3])
    pyl.sca(sub1)                
    pyl.plot(t,pos_error)
    pyl.xlabel('episodes')
    pyl.ylabel('pos error')
    pyl.sca(sub2) 
    pyl.plot(t,phi_error)
    pyl.xlabel('episodes')
    pyl.ylabel('phi error')
    pyl.sca(sub3) 
    pyl.plot(t,w_error)
    pyl.xlabel('episodes')
    pyl.ylabel('w error')
    pyl.sca(sub4) 
    pyl.plot(t,collision)
    pyl.xlabel('episodes')
    pyl.ylabel('collision count')
    pyl.show()  

if __name__ == '__main__':
    arglist = parse_args()
    if on_train:
        train(arglist)
    else:
        curve_plot(arglist)
