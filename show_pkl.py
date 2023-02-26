#show_pkl.py
 
import pickle
path='/home/changwon/detection_task/mmdetection3d/data/spa/spa_dbinfos_train.pkl'
#path='/home/changwon/detection_task/SSOD/kakao/test_1229/yc_ssda/data/waymo/waymo_processed_data_v0_5_0_waymo_dbinfos_train_sampled_1.pkl'
#path='/home/changwon/detection_task/frustum-convnet_modification/kitti/kitti/data/pickle_data/frustum_pedcyc_val_rgb_detection.pickle'
#f=open(path,'rb')
#data=pickle.load(f)


a=[]
with open(path, 'rb') as read:
    while True:
        try:
            a.append(pickle.load(read))
        except EOFError:
            break

import numpy as np
z_ = [i['annos']['location'] for i in a[0]]
z__ = np.concatenate(z_)
z_m = []
for aa in z__:
    if aa[0] == -1000. or aa[1] == -1000. or aa[2] == -1000:
        pass
    else:
        z_m.append(aa[1])



print(a)


