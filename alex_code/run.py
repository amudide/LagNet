import os
import numpy as np
import scanpy as sc
import argparse
import time
import ray
from ray import tune
import statistics

from models import *
from train import *
from utils import *

# ROOT_DIR = '/scratch1/alexwu/tf_lag'

def main():

	parser = argparse.ArgumentParser()
	parser.add_argument('-m','--method',dest='method',type=str,default='baseline')
	parser.add_argument('-ds','--dataset',dest='dataset',type=str)
	parser.add_argument('-tfp','--tf_path',dest='tf_path',type=str,default=None)
	parser.add_argument('-dyn','--dyn',dest='dynamics',type=str,default='pseudotime')
	parser.add_argument('-dev','--device',dest='device',type=str,default='cpu')
	parser.add_argument('-tn','--trial_no',dest='trial_no',type=int,default=0)
	parser.add_argument('-lmr','--lam_ridge',dest='lam_ridge',type=float,default=0.)
	parser.add_argument('-p','--penalty',dest='penalty',type=str,default="H")
	parser.add_argument('-l','--lag',dest='lag',type=int,default=5)
	parser.add_argument('-hd', '--hidden',dest='hidden',type=int,default=16)
	parser.add_argument('-mi','--max_iter',dest='max_iter',type=int,default=500)
	parser.add_argument('-lr','--learning_rate',dest='learning_rate',type=float,default=0.0001)
	parser.add_argument('-pr','--proba',dest='proba',type=int,default=0)
	parser.add_argument('-tol','--tolerance',dest='tolerance',type=float,default=0.01)
	parser.add_argument('-ce','--check_every',dest='check_every',type=int,default=100)
	parser.add_argument('-rd','--root_dir',dest='root_dir',type=str)

	args = parser.parse_args()

	data_dir = os.path.join(args.root_dir,'datasets','preprocessed')
	gc_dir = os.path.join(args.root_dir,'results',args.dataset,'gc')

	if not os.path.exists(os.path.join(args.root_dir,'results',args.dataset)):
		os.mkdir(os.path.join(args.root_dir,'results',args.dataset))
	if not os.path.exists(gc_dir):
		os.mkdir(gc_dir)

	# raw_adata = sc.read(os.path.join(raw_data_dir,'{}.h5ad'.format(args.dataset)))
	adata = sc.read(os.path.join(data_dir,'{}.h5ad'.format(args.dataset)))

	if args.dynamics == 'pseudotime':
		if 'dpt_pseudotime' not in adata.obs:
			print('Inferring pseudotime...')
			sc.tl.dpt(adata)
		knn_graph = adata.obsp['distances'].astype(bool).astype(float)
		A = dag_orient_edges(knn_graph,adata.obs['dpt_pseudotime'].values)
		A = torch.FloatTensor(A)
		A = normalize_adjacency(A.T).T # normalize w.r.t. lookback hops

	elif args.dynamics == 'rna_velocity':
		if 'velocity_transition' not in adata.uns:
			print('Inferring RNA velocity transition matrix...')
			vk = VelocityKernel(adata).compute_transition_matrix()
			A = vk.transition_matrix
		else:
			# normalize w.r.t. lookback hops
			A = adata.uns['velocity_transition']
			A = normalize_adjacency(torch.FloatTensor(A.toarray()))

		# transpose transitions (go backward in time)
		A = A.T # .toarray()

		# if proba is False (0), it won't use the probabilistic 
		# transition matrix
		if not args.proba:
			for i in range(len(A)):
				nzeros = []
				for j in range(len(A)):
					if A[i][j] > 0:
						nzeros.append(A[i][j])
				m = statistics.median(nzeros)
				for j in range(len(A)):
					if A[i][j] < m:
						A[i][j] = 0
					else:
						A[i][j] = 1
			A = torch.FloatTensor(A)
			A = normalize_adjacency(A)

	# perform diffusion
	print('Performing diffusion...')
	A = torch.FloatTensor(A)
	X = torch.FloatTensor(adata[:,adata.var['is_reg']].X.toarray())
	Y = torch.FloatTensor(adata[:,adata.var['is_target']].X.toarray())

	print('# of Regs: {}, # of Targets: {}'.format(X.shape[1],Y.shape[1]))

	AX = calculate_AX(A,X,args.lag)

	dir_name = '{}.trial{}.h{}.{}.lag{}.{}'.format(args.method,args.trial_no,args.hidden,
													  args.penalty,args.lag,args.dynamics)

	if not os.path.exists(os.path.join(gc_dir,dir_name)):
		os.mkdir(os.path.join(gc_dir,dir_name))

	ray.init(object_store_memory=10**9)

	total_start = time.time()
	lam_list = np.round(np.logspace(-1, 1, num=19),4).tolist()
	# lam_list = sorted(list(set(lam_list)))

	config = {'method': args.method,
			  'AX': AX,
			  'Y': Y,
			  'trial': args.trial_no,
			  'lr': args.learning_rate,
			  'lam': tune.grid_search(lam_list),
			  'lam_ridge': args.lam_ridge,
			  'penalty': args.penalty,
			  'lag': args.lag,
			  'hidden': [args.hidden],
			  'max_iter': args.max_iter,
			  'device': args.device,
			  'lookback': 5,
			  'check_every': args.check_every,
			  'verbose': True,
			  'tol': args.tolerance,
			  'dynamics': args.dynamics,
			  'gc_dir': gc_dir,
			  'dir_name': dir_name}

	resources_per_trial = {"cpu": 1, "gpu": 0.1, "memory": 2 * 1024 * 1024 * 1024}
	analysis = tune.run(train_model,resources_per_trial=resources_per_trial,config=config,
						local_dir=os.path.join(args.root_dir,'results'))
	
	print('Total time:',time.time()-total_start)
	np.savetxt(os.path.join(gc_dir,dir_name + '.time.txt'),np.array([time.time()-total_start]))

if __name__ == "__main__":
	main()
