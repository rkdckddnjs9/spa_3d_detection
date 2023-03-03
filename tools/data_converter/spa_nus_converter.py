# Copyright (c) OpenMMLab. All rights reserved.
import os
from collections import OrderedDict
from os import path as osp
from typing import List, Tuple, Union

import mmcv
import numpy as np
from nuscenes.nuscenes import NuScenes
from nuscenes.utils.geometry_utils import view_points
from pyquaternion import Quaternion
from shapely.geometry import MultiPoint, box

from mmdet3d.core.bbox import points_cam2img
from mmdet3d.datasets import NuScenesDataset
from pathlib import Path

nus_categories = ('car', 'bicycle', 'motorcycle', 'pedestrian')

nus_attributes = ('cycle.with_rider', 'cycle.without_rider',
                  'pedestrian.moving', 'pedestrian.standing',
                  'pedestrian.sitting_lying_down', 'vehicle.moving',
                  'vehicle.parked', 'vehicle.stopped', 'None', 'vehicle.bicycle')

def _read_imageset_file(path):
    with open(path, 'r') as f:
        lines = f.readlines()
    return [line.splitlines()[0] for line in lines]

def create_spa_nus_infos(root_path,
                          info_prefix,
                          version='v1.0-trainval',
                          max_sweeps=10):
    """Create info file of nuscene dataset.

    Given the raw data, generate its related info file in pkl format.

    Args:
        root_path (str): Path of the data root.
        info_prefix (str): Prefix of the info file to be generated.
        version (str, optional): Version of the data.
            Default: 'v1.0-trainval'.
        max_sweeps (int, optional): Max number of sweeps.
            Default: 10.
    """
    from nuscenes.nuscenes import NuScenes
    nusc = NuScenes(version=version, dataroot=root_path, verbose=True)
    from nuscenes.utils import splits
    available_vers = ['v1.0-spa-trainval', 'v1.0-test', 'v1.0-mini']
    assert version in available_vers
    if version == 'v1.0-trainval':
        train_scenes = splits.train
        val_scenes = splits.val
    elif version == 'v1.0-test':
        train_scenes = splits.test
        val_scenes = []
    elif version == 'v1.0-mini':
        train_scenes = splits.mini_train
        val_scenes = splits.mini_val
    elif version == "v1.0-spa-trainval":
        imageset_folder = Path(root_path) / 'ImageSets'
        train_img_ids = _read_imageset_file(str(imageset_folder / 'train.txt'))
        val_img_ids = _read_imageset_file(str(imageset_folder / 'val.txt'))
        #test_img_ids = _read_imageset_file(str(imageset_folder / 'test.txt'))
        train_scenes = train_img_ids
        val_scenes = val_img_ids
    else:
        raise ValueError('unknown')

    # filter existing scenes.
    # available_scenes = get_available_scenes(nusc)
    # available_scene_names = [s['name'] for s in available_scenes]
    # train_scenes = list(
    #     filter(lambda x: x in available_scene_names, train_scenes))
    # val_scenes = list(filter(lambda x: x in available_scene_names, val_scenes))
    # train_scenes = set([
    #     available_scenes[available_scene_names.index(s)]['token']
    #     for s in train_scenes
    # ])
    # val_scenes = set([
    #     available_scenes[available_scene_names.index(s)]['token']
    #     for s in val_scenes
    # ])

    test = 'test' in version
    if test:
        print('test scene: {}'.format(len(train_scenes)))
    else:
        print('train scene: {}, val scene: {}'.format(
            len(train_scenes), len(val_scenes)))
    train_nusc_infos, val_nusc_infos = _fill_trainval_infos(
        nusc, train_scenes, val_scenes, test, max_sweeps=max_sweeps)

    metadata = dict(version=version)
    if test:
        print('test sample: {}'.format(len(train_nusc_infos)))
        data = dict(infos=train_nusc_infos, metadata=metadata)
        info_path = osp.join(root_path,
                             '{}_infos_test.pkl'.format(info_prefix))
        mmcv.dump(data, info_path)
    else:
        print('train sample: {}, val sample: {}'.format(
            len(train_nusc_infos), len(val_nusc_infos)))
        data = dict(infos=train_nusc_infos, metadata=metadata)
        info_path = osp.join(root_path,
                             '{}_infos_train.pkl'.format(info_prefix))
        mmcv.dump(data, info_path)
        data['infos'] = val_nusc_infos
        info_val_path = osp.join(root_path,
                                 '{}_infos_val.pkl'.format(info_prefix))
        mmcv.dump(data, info_val_path)


def get_label_anno(label_path):
    annotations = {}
    annotations.update({
        'name': [],
        'track_id': [],
        'truncated': [],
        'occluded': [],
        'alpha': [],
        'bbox': [],
        'dimensions': [],
        'location': [],
        'rotation_y': []
    })
    name_map={'car':'car', 'motocycle':'motorcycle', 'pedestrian':'pedestrian','cyclist':'bicycle'}
    with open(label_path, 'r') as f:
        lines = f.readlines()
    # if len(lines) == 0 or len(lines[0]) < 15:
    #     content = []
    # else:
    content = [line.strip().split(' ') for line in lines]
    num_objects = len([x[0] for x in content if x[0] != 'DontCare'])
    # for x in content: #check for motocycle
    #     if x[0] == 'motocycle':
    #         print(1)
    # annotations['name'] = np.array([ 'motorcycle' if x[0] == 'motocycle' else x[0] for x in content])
    annotations['name'] = np.array([ name_map[x[0]] for x in content])
    num_gt = len(annotations['name'])
    annotations['track_id'] = np.array([x[1] for x in content])
    annotations['truncated'] = np.array([float(x[2]) for x in content])
    annotations['occluded'] = np.array([int(x[3]) for x in content])
    annotations['alpha'] = np.array([float(x[4]) for x in content])
    annotations['bbox'] = np.array([[float(info) for info in x[5:9]]
                                    for x in content]).reshape(-1, 4) #1102.00 110.00 1596.00 1203.00
    # no 2d box processing
    annotations['bbox'] = np.array([ [1102.00, 110.00, 1596.00, 1203.00]  if -99999.0 in i else i for i in annotations['bbox']]).reshape(-1, 4)


    # dimensions will convert hwl format to standard lhw(camera) format.
    annotations['dimensions'] = np.array([[float(info) for info in x[9:12]]
                                          for x in content
                                          ]).reshape(-1, 3)[:, [2, 1, 0]] #h, w, l -> l ,w, h
    annotations['location'] = np.array([[float(info) for info in x[12:15]]
                                        for x in content]).reshape(-1, 3)
    annotations['rotation_y'] = np.array([float(x[15])
                                          for x in content]).reshape(-1)
    if len(content) != 0 and len(content[0]) == 17:  # have score
        annotations['score'] = np.array([float(x[16]) for x in content])
    else:
        annotations['score'] = np.zeros((annotations['bbox'].shape[0], ))
    index = list(range(num_objects)) + [-1] * (num_gt - num_objects)
    annotations['index'] = np.array(index, dtype=np.int32)
    annotations['group_ids'] = np.arange(num_gt, dtype=np.int32)
    return annotations   

def box_center_to_corner_3d(centers, dims, angles):

    translation = centers[:, 0:3]
    # h, w, l = dims[:, 0], dims[:, 1], dims[:, 2]
    l, w, h = dims[:, 0], dims[:, 1], dims[:, 2]
    rotation = angles + np.pi/2

    # Create a bounding box outline
    x_corners = np.array([[l_ / 2, l_ / 2, -l_ / 2, -l_ / 2, l_ / 2, l_ / 2, -l_ / 2, -l_ / 2] for l_ in l])
    #[[l_ / 2, l_ / 2, -l_ / 2, -l_ / 2, l_ / 2, l_ / 2, -l_ / 2, -l_ / 2] for l_ in l]
    y_corners = np.array([[w_ / 2, w_ / 2, -w_ / 2, -w_ / 2, w_ / 2, w_ / 2, -w_ / 2, -w_ / 2] for w_ in w])
    #[w / 2, -w / 2, -w / 2, w / 2, w / 2, -w / 2, -w / 2, w / 2]
    z_corners = np.array([[h_ / 2, h_ / 2, -h_ / 2, -h_ / 2, h_ / 2, h_ / 2, -h_ / 2, -h_ / 2] for h_ in h])
    #[-h/2, -h/2, -h/2, -h/2, h/2, h/2, h/2, h/2] #[0, 0, 0, 0, -h, -h, -h, -h]
    bounding_box = np.array([np.vstack([x_corners[i], y_corners[i], z_corners[i]]) for i in range(x_corners.shape[0])])


    # rotation_matrix = np.array([[np.cos(rotation),  0,  np.sin(rotation)],
    #                      [0,  1,  0],
    #                      [-np.sin(rotation), 0,  np.cos(rotation)]])
    rotation_matrix = np.array([np.array([[np.cos(rotation_),  -np.sin(rotation_), 0],
                                            [np.sin(rotation_), np.cos(rotation_), 0],
                                            [0,  0,  1]]) for rotation_ in rotation])


    corner_box = np.array([np.dot(rotation_matrix[i], bounding_box[i]).T + translation[i] for i in range(x_corners.shape[0])])
    # corner_box = corner_box[:, [2, 0, 1]] * np.array([[1, -1, -1]])
    #corner_box = corner_box[:, [2, 0, 1]] * np.array([[1, -1, -1]])

    # return corner_box.transpose()
    return corner_box

def get_pts_in_3dbox(pc, corners):
    num_pts_in_gt = []
    for num, corner in enumerate(corners):
        x_max, x_min = corner[:, 0].max(), corner[:, 0].min()
        y_max, y_min = corner[:, 1].max(), corner[:, 1].min()
        z_max, z_min = corner[:, 2].max(), corner[:, 2].min()
        
        count = 0
        for point in pc:
            if (x_min<= point[0] <= x_max and \
                 y_min <= point[1] <= y_max and \
                 z_min <= point[2] <= z_max):
                count += 1
        
        num_pts_in_gt.append(count)
    return num_pts_in_gt

def get_pts_in_3dbox_(pc, corners):
    num_pts_in_gt = []
    for num, corner in enumerate(corners):
        x_max, x_min = corner[:, 0].max(), corner[:, 0].min()
        y_max, y_min = corner[:, 1].max(), corner[:, 1].min()
        z_max, z_min = corner[:, 2].max(), corner[:, 2].min()

        mask_x = np.logical_and(pc[:,0] >= x_min, pc[:, 0] <= x_max)
        mask_y = np.logical_and(pc[:,1] >= y_min, pc[:, 1] <= y_max)
        mask_z = np.logical_and(pc[:,2] >= z_min, pc[:, 2] <= z_max)
        mask = mask_x * mask_y * mask_z
        num_pts_in_gt.append(mask.sum())
    
    return num_pts_in_gt

def get_pts_index_in_3dbox_(pc, corners):
    num_pts_in_gt = []
    for num, corner in enumerate(corners):
        x_max, x_min = corner[:, 0].max(), corner[:, 0].min()
        y_max, y_min = corner[:, 1].max(), corner[:, 1].min()
        z_max, z_min = corner[:, 2].max(), corner[:, 2].min()

        mask_x = np.logical_and(pc[:,0] >= x_min, pc[:, 0] <= x_max)
        mask_y = np.logical_and(pc[:,1] >= y_min, pc[:, 1] <= y_max)
        mask_z = np.logical_and(pc[:,2] >= z_min, pc[:, 2] <= z_max)
        mask = mask_x * mask_y * mask_z
        num_pts_in_gt.append(mask)
    
    return num_pts_in_gt

def _fill_trainval_infos(nusc,
                         train_scenes,
                         val_scenes,
                         test=False,
                         max_sweeps=10):
    """Generate the train/val infos from the raw data.

    Args:
        nusc (:obj:`NuScenes`): Dataset class in the nuScenes dataset.
        train_scenes (list[str]): Basic information of training scenes.
        val_scenes (list[str]): Basic information of validation scenes.
        test (bool, optional): Whether use the test mode. In test mode, no
            annotations can be accessed. Default: False.
        max_sweeps (int, optional): Max number of sweeps. Default: 10.

    Returns:
        tuple[list[dict]]: Information of training set and validation set
            that will be saved to the info file.
    """
    train_nusc_infos = []
    val_nusc_infos = []
    root_path = Path("./data/spa/")
    for path in mmcv.track_iter_progress(train_scenes):
        place = path.split("*")[0]
        scene = path.split("*")[1]
        frame = path.split("*")[2]
        token = path
        
        calib_path = root_path / place / scene / "calib" / "{}.txt".format(frame)
        # if calib_path is not None:
        #     with open(calib_path, 'r') as f:
        #         lines = f.read().splitlines()
        #         P0 = np.array([float(info) for info in lines[0].split(' ')[1:13]
        #                     ]).reshape([3, 4])
        #         P1 = np.array([float(info) for info in lines[1].split(' ')[1:13]
        #                     ]).reshape([3, 4])
        #         P2 = np.array([float(info) for info in lines[2].split(' ')[1:13]
        #                     ]).reshape([3, 4])
        #         P3 = np.array([float(info) for info in lines[3].split(' ')[1:13]
        #                     ]).reshape([3, 4])
        #         P4 = np.array([float(info) for info in lines[4].split(' ')[1:13]
        #                     ]).reshape([3, 4])
        # projection_matrix = [P0, P1, P2, P3, P4]
        if calib_path is not None:
            with open(calib_path, 'r') as f:
                lines = f.read().splitlines()
                P0_intrinsic = np.array([float(info) for info in lines[0].split(' ')[1:10]
                            ]).reshape([3, 3])
                P1_intrinsic = np.array([float(info) for info in lines[1].split(' ')[1:10]
                            ]).reshape([3, 3])
                P2_intrinsic = np.array([float(info) for info in lines[2].split(' ')[1:10]
                            ]).reshape([3, 3])
                P3_intrinsic = np.array([float(info) for info in lines[3].split(' ')[1:10]
                            ]).reshape([3, 3])
                P4_intrinsic = np.array([float(info) for info in lines[4].split(' ')[1:10]
                            ]).reshape([3, 3])
                P0_extrinsic = np.array([float(info) for info in lines[5].split(' ')[1:13]
                            ]).reshape([3, 4])
                P1_extrinsic = np.array([float(info) for info in lines[6].split(' ')[1:13]
                            ]).reshape([3, 4])
                P2_extrinsic = np.array([float(info) for info in lines[7].split(' ')[1:13]
                            ]).reshape([3, 4])
                P3_extrinsic = np.array([float(info) for info in lines[8].split(' ')[1:13]
                            ]).reshape([3, 4])
                P4_extrinsic = np.array([float(info) for info in lines[9].split(' ')[1:13]
                            ]).reshape([3, 4])
        # projection_matrix = [P0, P1, P2, P3, P4]
        projection_matrix = [ [P0_intrinsic, P0_extrinsic], \
                              [P1_intrinsic, P1_extrinsic], \
                              [P2_intrinsic, P2_extrinsic], \
                              [P3_intrinsic, P3_extrinsic], \
                              [P4_intrinsic, P4_extrinsic], 
            ]

        velo_path = root_path / place / scene / "velo/bin/data" / "{}.bin".format(frame)

        # lidar_token = sample['data']['LIDAR_TOP']
        # sd_rec = nusc.get('sample_data', sample['data']['LIDAR_TOP'])
        # cs_record = nusc.get('calibrated_sensor',
        #                      sd_rec['calibrated_sensor_token'])
        # pose_record = nusc.get('ego_pose', sd_rec['ego_pose_token'])
        # lidar_path, boxes, _ = nusc.get_sample_data(lidar_token)

        mmcv.check_file_exist(velo_path)

        info = {
            'lidar_path': velo_path,
            'token': token,
            'sweeps': [],
            'cams': dict(),
            # 'lidar2ego_translation': cs_record['translation'],
            # 'lidar2ego_rotation': cs_record['rotation'],
            # 'ego2global_translation': pose_record['translation'],
            # 'ego2global_rotation': pose_record['rotation'],
            # 'timestamp': sample['timestamp'],
        }

        # obtain 6 image's information per frame
        camera_types = [
            'CAM_FRONT',
            'CAM_FRONT_RIGHT',
            'CAM_FRONT_LEFT',
            # 'CAM_BACK',
            'CAM_BACK_LEFT',
            'CAM_BACK_RIGHT',
        ] # 5: front, 4: front_left, 3: front_right, 2: back_right, 1:back_right

        camera_mapping = {4:'CAM_FRONT', 1:'CAM_FRONT_LEFT', 5:'CAM_FRONT_RIGHT', 3:'CAM_BACK_RIGHT',  2:'CAM_BACK_LEFT'}

        cam_num_list = [1, 2, 3, 4, 5]
        anno_path_ = []
        cam_path_ = []
        for num in cam_num_list:
            anno_path_.append(root_path / place / scene / "label/label_{}".format(num) / "{}.txt".format(frame))
            cam_path_.append(root_path / place / scene / "cam_img/{}".format(num) / "data_rgb" / "{}.png".format(frame))

            
        for num, cam in enumerate(cam_num_list):
            # cam_num = num+1
            # cam_path, _, cam_intrinsic = nusc.get_sample_data(cam_token)
            # cam_info = obtain_sensor2top(nusc, cam_token, l2e_t, l2e_r_mat,
            #                              e2g_t, e2g_r_mat, cam)
            cam_info = {'data_path':cam_path_[num], 'type': camera_mapping[cam]}
            cam_info.update(cam_intrinsic=projection_matrix[num])
            info['cams'].update({camera_mapping[cam]: cam_info})

        # obtain sweeps for a single key-frame
        # sd_rec = nusc.get('sample_data', sample['data']['LIDAR_TOP'])
        sweeps = []
        # while len(sweeps) < max_sweeps:
        #     if not sd_rec['prev'] == '':
        #         sweep = obtain_sensor2top(nusc, sd_rec['prev'], l2e_t,
        #                                   l2e_r_mat, e2g_t, e2g_r_mat, 'lidar')
        #         sweeps.append(sweep)
        #         sd_rec = nusc.get('sample_data', sd_rec['prev'])
        #     else:
        #         break
        info['sweeps'] = sweeps
        # obtain annotation
        if not test:
            data_anno = {}
            for id, a_path in enumerate(anno_path_):
                anno_ = get_label_anno(a_path)
                if id == 0:
                    for key in anno_.keys():
                        data_anno[key] = anno_[key]
                    data_anno['mask'] = np.full(len(data_anno['name']), id)
                else:
                    for key in anno_.keys():
                        if key in ['bbox', 'dimensions', 'location']:
                            data_anno[key] = np.vstack((data_anno[key], anno_[key]))
                        else:
                            data_anno[key] = np.hstack((data_anno[key], anno_[key]))
                    mask = np.full(len(anno_['name']), id)
                    data_anno['mask'] = np.hstack((data_anno['mask'], mask))
            
            # remove overlapping annos
            _, mask = np.unique(data_anno['track_id'], return_inverse=True)
            mask_ = mask[:_.shape[0]]
            for key in list(data_anno.keys()):
                data_anno[key] = data_anno[key][mask_]

            locs = np.array(data_anno['location']).reshape(-1, 3)
            dims = np.array(data_anno['dimensions']).reshape(-1, 3) # order : l,w,h 
            # rots = np.array([b.orientation.yaw_pitch_roll[0] for b in ref_boxes]).reshape(-1, 1)
            #velocity = np.array([b.velocity for b in ref_boxes]).reshape(-1, 3)
            rots = np.array(data_anno['rotation_y']).reshape(
                -1, 1
            )
            names = np.array(data_anno['name'])
            tokens = np.array(token)

            gt_boxes = np.concatenate(
                [locs, dims, rots], axis=1
            )
            # gt_boxes = np.concatenate([locs, dims, rots], axis=1

            gt_boxes_corners = box_center_to_corner_3d(gt_boxes[:, :3], gt_boxes[:, 3:6], gt_boxes[:, 6])
            points = np.fromfile(velo_path, dtype=np.float32, count=-1).reshape([-1, 4]) # 4 : x, y, z, intensity
            num_pts_list = get_pts_in_3dbox_(points, gt_boxes_corners)

            data_anno['num_lidar_pts'] = np.array(num_pts_list)

            # num_gt_points == 0 !!!!
            valid_flag = (data_anno['num_lidar_pts'] >0)


            # we need to convert box size to
            # the format of our lidar coordinate system
            # which is x_size, y_size, z_size (corresponding to l, w, h)

            info['gt_boxes'] = gt_boxes
            info['gt_names'] = names
            # info['gt_velocity'] = velocity.reshape(-1, 2)
            info['num_lidar_pts'] = data_anno['num_lidar_pts']
            # info['num_radar_pts'] = np.array(
            #     [a['num_radar_pts'] for a in annotations])
            info['valid_flag'] = valid_flag
            info['cam_mask'] = data_anno['mask']

            train_nusc_infos.append(info)

    for path in mmcv.track_iter_progress(val_scenes):
        place = path.split("*")[0]
        scene = path.split("*")[1]
        frame = path.split("*")[2]
        token = path
        
        calib_path = root_path / place / scene / "calib" / "{}.txt".format(frame)
        # if calib_path is not None:
        #     with open(calib_path, 'r') as f:
        #         lines = f.read().splitlines()
        #         P0 = np.array([float(info) for info in lines[0].split(' ')[1:13]
        #                     ]).reshape([3, 4])
        #         P1 = np.array([float(info) for info in lines[1].split(' ')[1:13]
        #                     ]).reshape([3, 4])
        #         P2 = np.array([float(info) for info in lines[2].split(' ')[1:13]
        #                     ]).reshape([3, 4])
        #         P3 = np.array([float(info) for info in lines[3].split(' ')[1:13]
        #                     ]).reshape([3, 4])
        #         P4 = np.array([float(info) for info in lines[4].split(' ')[1:13]
        #                     ]).reshape([3, 4])
        # projection_matrix = [P0, P1, P2, P3, P4]
        if calib_path is not None:
            with open(calib_path, 'r') as f:
                lines = f.read().splitlines()
                P0_intrinsic = np.array([float(info) for info in lines[0].split(' ')[1:10]
                            ]).reshape([3, 3])
                P1_intrinsic = np.array([float(info) for info in lines[1].split(' ')[1:10]
                            ]).reshape([3, 3])
                P2_intrinsic = np.array([float(info) for info in lines[2].split(' ')[1:10]
                            ]).reshape([3, 3])
                P3_intrinsic = np.array([float(info) for info in lines[3].split(' ')[1:10]
                            ]).reshape([3, 3])
                P4_intrinsic = np.array([float(info) for info in lines[4].split(' ')[1:10]
                            ]).reshape([3, 3])
                P0_extrinsic = np.array([float(info) for info in lines[5].split(' ')[1:13]
                            ]).reshape([3, 4])
                P1_extrinsic = np.array([float(info) for info in lines[6].split(' ')[1:13]
                            ]).reshape([3, 4])
                P2_extrinsic = np.array([float(info) for info in lines[7].split(' ')[1:13]
                            ]).reshape([3, 4])
                P3_extrinsic = np.array([float(info) for info in lines[8].split(' ')[1:13]
                            ]).reshape([3, 4])
                P4_extrinsic = np.array([float(info) for info in lines[9].split(' ')[1:13]
                            ]).reshape([3, 4])
        # projection_matrix = [P0, P1, P2, P3, P4]
        projection_matrix = [ [P0_intrinsic, P0_extrinsic], \
                              [P1_intrinsic, P1_extrinsic], \
                              [P2_intrinsic, P2_extrinsic], \
                              [P3_intrinsic, P3_extrinsic], \
                              [P4_intrinsic, P4_extrinsic], 
            ]

        velo_path = root_path / place / scene / "velo/bin/data" / "{}.bin".format(frame)

        # lidar_token = sample['data']['LIDAR_TOP']
        # sd_rec = nusc.get('sample_data', sample['data']['LIDAR_TOP'])
        # cs_record = nusc.get('calibrated_sensor',
        #                      sd_rec['calibrated_sensor_token'])
        # pose_record = nusc.get('ego_pose', sd_rec['ego_pose_token'])
        # lidar_path, boxes, _ = nusc.get_sample_data(lidar_token)

        mmcv.check_file_exist(velo_path)

        info = {
            'lidar_path': velo_path,
            'token': token,
            'sweeps': [],
            'cams': dict(),
            # 'lidar2ego_translation': cs_record['translation'],
            # 'lidar2ego_rotation': cs_record['rotation'],
            # 'ego2global_translation': pose_record['translation'],
            # 'ego2global_rotation': pose_record['rotation'],
            # 'timestamp': sample['timestamp'],
        }

        # obtain 6 image's information per frame
        camera_types = [
            'CAM_FRONT',
            'CAM_FRONT_RIGHT',
            'CAM_FRONT_LEFT',
            # 'CAM_BACK',
            'CAM_BACK_LEFT',
            'CAM_BACK_RIGHT',
        ]
        camera_mapping = {4:'CAM_FRONT', 1:'CAM_FRONT_LEFT', 5:'CAM_FRONT_RIGHT', 3:'CAM_BACK_RIGHT',  2:'CAM_BACK_LEFT'}

        cam_num_list = [1, 2, 3, 4, 5]
        anno_path_ = []
        cam_path_ = []
        for num in cam_num_list:
            anno_path_.append(root_path / place / scene / "label/label_{}".format(num) / "{}.txt".format(frame))
            cam_path_.append(root_path / place / scene / "cam_img/{}".format(num) / "data_rgb" / "{}.png".format(frame))

            
        for num, cam in enumerate(cam_num_list):
            # cam_num = num+1
            # cam_path, _, cam_intrinsic = nusc.get_sample_data(cam_token)
            # cam_info = obtain_sensor2top(nusc, cam_token, l2e_t, l2e_r_mat,
            #                              e2g_t, e2g_r_mat, cam)
            cam_info = {'data_path':cam_path_[num], 'type': camera_mapping[cam]}
            cam_info.update(cam_intrinsic=projection_matrix[num])
            info['cams'].update({camera_mapping[cam]: cam_info})

        # obtain sweeps for a single key-frame
        # sd_rec = nusc.get('sample_data', sample['data']['LIDAR_TOP'])
        sweeps = []
        # while len(sweeps) < max_sweeps:
        #     if not sd_rec['prev'] == '':
        #         sweep = obtain_sensor2top(nusc, sd_rec['prev'], l2e_t,
        #                                   l2e_r_mat, e2g_t, e2g_r_mat, 'lidar')
        #         sweeps.append(sweep)
        #         sd_rec = nusc.get('sample_data', sd_rec['prev'])
        #     else:
        #         break
        info['sweeps'] = sweeps
        # obtain annotation
        if not test:
            data_anno = {}
            for id, a_path in enumerate(anno_path_):
                anno_ = get_label_anno(a_path)
                if id == 0:
                    for key in anno_.keys():
                        data_anno[key] = anno_[key]
                    data_anno['mask'] = np.full(len(data_anno['name']), id)
                else:
                    for key in anno_.keys():
                        if key in ['bbox', 'dimensions', 'location']:
                            data_anno[key] = np.vstack((data_anno[key], anno_[key]))
                        else:
                            data_anno[key] = np.hstack((data_anno[key], anno_[key]))
                    mask = np.full(len(anno_['name']), id)
                    data_anno['mask'] = np.hstack((data_anno['mask'], mask))
            locs = np.array(data_anno['location']).reshape(-1, 3)
            dims = np.array(data_anno['dimensions']).reshape(-1, 3) # order : l,w,h 
            # rots = np.array([b.orientation.yaw_pitch_roll[0] for b in ref_boxes]).reshape(-1, 1)
            #velocity = np.array([b.velocity for b in ref_boxes]).reshape(-1, 3)
            rots = np.array(data_anno['rotation_y']).reshape(
                -1, 1
            )
            names = np.array(data_anno['name'])
            tokens = np.array(token)

            gt_boxes = np.concatenate(
                [locs, dims, rots], axis=1
            )
            # gt_boxes = np.concatenate([locs, dims, rots], axis=1

            gt_boxes_corners = box_center_to_corner_3d(gt_boxes[:, :3], gt_boxes[:, 3:6], gt_boxes[:, 6])
            points = np.fromfile(velo_path, dtype=np.float32, count=-1).reshape([-1, 4]) # 4 : x, y, z, intensity
            num_pts_list = get_pts_in_3dbox_(points, gt_boxes_corners)

            data_anno['num_lidar_pts'] = np.array(num_pts_list)

            # num_gt_points == 0 !!!!
            valid_flag = (data_anno['num_lidar_pts'] >0)


            # we need to convert box size to
            # the format of our lidar coordinate system
            # which is x_size, y_size, z_size (corresponding to l, w, h)

            info['gt_boxes'] = gt_boxes
            info['gt_names'] = names
            # info['gt_velocity'] = velocity.reshape(-1, 2)
            info['num_lidar_pts'] = data_anno['num_lidar_pts']
            # info['num_radar_pts'] = np.array(
            #     [a['num_radar_pts'] for a in annotations])
            info['valid_flag'] = valid_flag
            info['cam_mask'] = data_anno['mask']
            val_nusc_infos.append(info)

    return train_nusc_infos, val_nusc_infos


def box_center_to_corner_3d_(box_center):
    # To return
    corner_boxes = np.zeros((8, 3))

    translation = box_center[0:3]
    l, w, h = box_center[3], box_center[4], box_center[5]
    rotation = box_center[6]

    # Create a bounding box outline
    # bounding_box = np.array([
    #     [-l/2, -l/2, l/2, l/2, -l/2, -l/2, l/2, l/2],
    #     [w/2, -w/2, -w/2, w/2, w/2, -w/2, -w/2, w/2],
    #     [-h/2, -h/2, -h/2, -h/2, h/2, h/2, h/2, h/2]])
    x_corners = [l / 2, l / 2, -l / 2, -l / 2, l / 2, l / 2, -l / 2, -l / 2]
    y_corners = [w / 2, -w / 2, -w / 2, w / 2, w / 2, -w / 2, -w / 2, w / 2]
    z_corners = [-h/2, -h/2, -h/2, -h/2, h/2, h/2, h/2, h/2] #[0, 0, 0, 0, -h, -h, -h, -h]
    bounding_box = np.vstack([x_corners, y_corners, z_corners])


    # rotation_matrix = np.array([[np.cos(rotation),  0,  np.sin(rotation)],
    #                      [0,  1,  0],
    #                      [-np.sin(rotation), 0,  np.cos(rotation)]])
    rotation_matrix = np.array([[np.cos(rotation),  -np.sin(rotation), 0],
                                [np.sin(rotation), np.cos(rotation), 0],
                                [0,  0,  1]])


    corner_box = (np.dot(rotation_matrix, bounding_box).T + translation).T
    # corner_box = corner_box[:, [2, 0, 1]] * np.array([[1, -1, -1]])
    #corner_box = corner_box[:, [2, 0, 1]] * np.array([[1, -1, -1]])

    # return corner_box.transpose()
    return corner_box

def obtain_sensor2top(nusc,
                      sensor_token,
                      l2e_t,
                      l2e_r_mat,
                      e2g_t,
                      e2g_r_mat,
                      sensor_type='lidar'):
    """Obtain the info with RT matric from general sensor to Top LiDAR.

    Args:
        nusc (class): Dataset class in the nuScenes dataset.
        sensor_token (str): Sample data token corresponding to the
            specific sensor type.
        l2e_t (np.ndarray): Translation from lidar to ego in shape (1, 3).
        l2e_r_mat (np.ndarray): Rotation matrix from lidar to ego
            in shape (3, 3).
        e2g_t (np.ndarray): Translation from ego to global in shape (1, 3).
        e2g_r_mat (np.ndarray): Rotation matrix from ego to global
            in shape (3, 3).
        sensor_type (str, optional): Sensor to calibrate. Default: 'lidar'.

    Returns:
        sweep (dict): Sweep information after transformation.
    """
    sd_rec = nusc.get('sample_data', sensor_token)
    cs_record = nusc.get('calibrated_sensor',
                         sd_rec['calibrated_sensor_token'])
    pose_record = nusc.get('ego_pose', sd_rec['ego_pose_token'])
    data_path = str(nusc.get_sample_data_path(sd_rec['token']))
    if os.getcwd() in data_path:  # path from lyftdataset is absolute path
        data_path = data_path.split(f'{os.getcwd()}/')[-1]  # relative path
    sweep = {
        'data_path': data_path,
        'type': sensor_type,
        'sample_data_token': sd_rec['token'],
        'sensor2ego_translation': cs_record['translation'],
        'sensor2ego_rotation': cs_record['rotation'],
        'ego2global_translation': pose_record['translation'],
        'ego2global_rotation': pose_record['rotation'],
        'timestamp': sd_rec['timestamp']
    }
    l2e_r_s = sweep['sensor2ego_rotation']
    l2e_t_s = sweep['sensor2ego_translation']
    e2g_r_s = sweep['ego2global_rotation']
    e2g_t_s = sweep['ego2global_translation']

    # obtain the RT from sensor to Top LiDAR
    # sweep->ego->global->ego'->lidar
    l2e_r_s_mat = Quaternion(l2e_r_s).rotation_matrix
    e2g_r_s_mat = Quaternion(e2g_r_s).rotation_matrix
    R = (l2e_r_s_mat.T @ e2g_r_s_mat.T) @ (
        np.linalg.inv(e2g_r_mat).T @ np.linalg.inv(l2e_r_mat).T)
    T = (l2e_t_s @ e2g_r_s_mat.T + e2g_t_s) @ (
        np.linalg.inv(e2g_r_mat).T @ np.linalg.inv(l2e_r_mat).T)
    T -= e2g_t @ (np.linalg.inv(e2g_r_mat).T @ np.linalg.inv(l2e_r_mat).T
                  ) + l2e_t @ np.linalg.inv(l2e_r_mat).T
    sweep['sensor2lidar_rotation'] = R.T  # points @ R.T + T
    sweep['sensor2lidar_translation'] = T
    return sweep

def generate_record_(ann_rec: dict, x1: float, y1: float, x2: float, y2: float,
                    sample_data_token: str, filename: str) -> OrderedDict:
    """Generate one 2D annotation record given various information on top of
    the 2D bounding box coordinates.

    Args:
        ann_rec (dict): Original 3d annotation record.
        x1 (float): Minimum value of the x coordinate.
        y1 (float): Minimum value of the y coordinate.
        x2 (float): Maximum value of the x coordinate.
        y2 (float): Maximum value of the y coordinate.
        sample_data_token (str): Sample data token.
        filename (str):The corresponding image file where the annotation
            is present.

    Returns:
        dict: A sample 2D annotation record.
            - file_name (str): file name
            - image_id (str): sample data token
            - area (float): 2d box area
            - category_name (str): category name
            - category_id (int): category id
            - bbox (list[float]): left x, top y, dx, dy of 2d box
            - iscrowd (int): whether the area is crowd
    """
    repro_rec = OrderedDict()
    repro_rec['sample_data_token'] = sample_data_token
    coco_rec = dict()

    relevant_keys = [
        'attribute_tokens',
        'category_name',
        'instance_token',
        'next',
        'num_lidar_pts',
        'num_radar_pts',
        'prev',
        'sample_annotation_token',
        'sample_data_token',
        'visibility_token',
    ]

    for key, value in ann_rec.items():
        if key in relevant_keys:
            repro_rec[key] = value

    repro_rec['bbox_corners'] = [x1, y1, x2, y2]
    repro_rec['filename'] = filename

    coco_rec['file_name'] = filename
    coco_rec['image_id'] = sample_data_token
    coco_rec['area'] = (y2 - y1) * (x2 - x1)

    # if repro_rec['category_name'] not in NuScenesDataset.NameMapping:
    #     return None
    cat_name = ann_rec['attribute_name']
    coco_rec['category_name'] = cat_name
    coco_rec['category_id'] = nus_categories.index(cat_name)
    coco_rec['bbox'] = [x1, y1, x2 - x1, y2 - y1]
    coco_rec['iscrowd'] = 0

    return coco_rec

def export_2d_annotation(root_path, info_path, version, mono3d=True):
    """Export 2d annotation from the info file and raw data.

    Args:
        root_path (str): Root path of the raw data.
        info_path (str): Path of the info file.
        version (str): Dataset version.
        mono3d (bool, optional): Whether to export mono3d annotation.
            Default: True.
    """
    # get bbox annotations for camera
    camera_types = [
        'CAM_FRONT',
        'CAM_FRONT_RIGHT',
        'CAM_FRONT_LEFT',
        # 'CAM_BACK',
        'CAM_BACK_LEFT',
        'CAM_BACK_RIGHT',
    ]
    nusc_infos = mmcv.load(info_path)['infos']
    nusc = NuScenes(version=version, dataroot=root_path, verbose=True)
    # info_2d_list = []
    cat2Ids = [
        dict(id=nus_categories.index(cat_name), name=cat_name)
        for cat_name in nus_categories
    ]
    coco_ann_id = 0
    coco_2d_dict = dict(annotations=[], images=[], categories=cat2Ids)
    for info in mmcv.track_iter_progress(nusc_infos):
        for cam in camera_types:
            cam_info = info['cams'][cam]
            coco_infos = get_2d_boxes(
                nusc,
                cam_info,
                info,
                visibilities=['', '1', '2', '3', '4'],
                mono3d=mono3d)
            (height, width, _) = mmcv.imread(cam_info['data_path']).shape
            coco_2d_dict['images'].append(
                dict(
                    file_name=str(cam_info['data_path']),
                    id=str(cam_info['data_path']),
                    token=info['token'],
                    # cam2ego_rotation=cam_info['sensor2ego_rotation'],
                    # cam2ego_translation=cam_info['sensor2ego_translation'],
                    # ego2global_rotation=info['ego2global_rotation'],
                    # ego2global_translation=info['ego2global_translation'],
                    cam_intrinsic=cam_info['cam_intrinsic'],
                    width=width,
                    height=height))
            for coco_info in coco_infos:
                if coco_info is None:
                    continue
                # add an empty key for coco format
                coco_info['segmentation'] = []
                coco_info['id'] = coco_ann_id
                coco_2d_dict['annotations'].append(coco_info)
                coco_ann_id += 1
    if mono3d:
        json_prefix = f'{info_path[:-4]}_mono3d'
    else:
        json_prefix = f'{info_path[:-4]}'
    mmcv.dump(coco_2d_dict, f'{json_prefix}.coco.json')


def get_2d_boxes(nusc,
                 cam_lnfo,
                 info,
                 visibilities: List[str],
                 mono3d=True):
    """Get the 2D annotation records for a given `sample_data_token`.

    Args:
        sample_data_token (str): Sample data token belonging to a camera
            keyframe.
        visibilities (list[str]): Visibility filter.
        mono3d (bool): Whether to get boxes with mono3d annotation.

    Return:
        list[dict]: List of 2D annotation record that belongs to the input
            `sample_data_token`.
    """
    cam_num = str(cam_lnfo['data_path']).split("/")[5]
    root_path = './data/spa/'
    place = str(cam_lnfo['data_path']).split("/")[2]
    scene = str(cam_lnfo['data_path']).split("/")[3]
    frame = str(cam_lnfo['data_path']).split("/")[-1].split(".")[0]
    label_path = root_path + place + "/" + scene + "/label/label_{}/".format(cam_num) + frame + ".txt"
    label_ = get_label_anno(label_path)
    velo_path = root_path +'/'+ place +'/'+ scene +'/'+ "velo/bin/data/{}.bin".format(frame)
    points = np.fromfile(velo_path, dtype=np.float32, count=-1).reshape([-1, 4]) 

    repro_recs = []
    for i in range(label_['name'].shape[0]):
        ann_rec = {}
        ann_rec['token'] = '{}*{}*{}'.format(place, scene, frame)
        ann_rec['sample_token'] = frame
        ann_rec['instance_token'] = frame
        ann_rec['visibility_token'] = 1 #label_['occluded'][num]
        ann_rec['attribute_tokens'] = frame
        ann_rec['translation'] = label_['location'][i].tolist()
        ann_rec['size'] = label_['dimensions'][i].tolist()
        ann_rec['rotation'] = list(Quaternion(axis=[0, 0, 1], radians=label_['rotation_y'][i]+np.pi/2))
        ann_rec['prev'] = ""
        ann_rec['next'] = ""
        
        # for pts in 3dbox
        gt_boxes = ann_rec['translation'] + ann_rec['size'] + [label_['rotation_y'][i]+np.pi/2]
        corners_3d = box_center_to_corner_3d_(gt_boxes)
        num_pts_list = get_pts_in_3dbox_(points, np.expand_dims(corners_3d, axis=0))
        ann_rec['num_lidar_pts'] = num_pts_list[0]
        ann_rec['attribute_name'] = label_['name'][i]
        ann_rec['attribute_id'] = label_['name'][i]


        # 3d box corners on lidar coordinage to 3d box corners on image coordinate
        # Generate dictionary record to be included in the .json file.
        gt_boxes_ = np.array(ann_rec['translation'] + ann_rec['size'] + [label_['rotation_y'][i]])
        corners_3d = box_center_to_corner_3d_(gt_boxes_)

        # in_front = np.argwhere(corners_3d[2, :] >0).flatten()
        # corners_3d = corners_3d[:, in_front]

        #3d box projection
        intrinsic = cam_lnfo['cam_intrinsic'][0]
        extrinsic = cam_lnfo['cam_intrinsic'][1][:3, :3]
        translation = cam_lnfo['cam_intrinsic'][1][:3, 3]
        corners_3d = np.dot(extrinsic, corners_3d) + translation.reshape(3, 1)
        corners_2d = np.dot(intrinsic, corners_3d).T
        corners_2d[:, 0] /= corners_2d[:, 2]
        corners_2d[:, 1] /= corners_2d[:, 2]
        corners_2d[:, 0] = np.clip(corners_2d[:, 0], 0, 1920)
        corners_2d[:, 1] = np.clip(corners_2d[:, 1], 0, 1200)


        min_x, max_x = int(corners_2d[:, 0].min()), int(corners_2d[:, 0].max())
        min_y, max_y = int(corners_2d[:, 1].min()), int(corners_2d[:, 1].max())
        
        repro_rec = generate_record_(ann_rec, min_x, min_y, max_x, max_y,
                                    ann_rec['token'], str(cam_lnfo['data_path']))

        # If mono3d=True, add 3D annotations in camera coordinates
        if mono3d and (repro_rec is not None):

            # 3dbox center projection
            center3d = np.array(ann_rec['translation']).reshape([1, 3]).T
            center3d = np.dot(extrinsic, center3d) + translation.reshape(3, 1)
            center2d = np.dot(intrinsic, center3d).T
            center2d[:, 0] /= center2d[:, 2]
            center2d[:, 1] /= center2d[:, 2]
            # center2d[:, 0] = np.clip(center2d[:, 0], 0, 1920)
            # center2d[:, 1] = np.clip(center2d[:, 1], 0, 1200)

            repro_rec['center2d'] = center2d.squeeze().tolist()
            # normalized center2D + depth
            # if samples with depth < 0 will be removed
            if repro_rec['center2d'][2] <= 0:
                pass
                

            # ann_token = ann_rec['attribute_tokens']
            DefaultAttribute = {
                'car': 'vehicle.parked',
                'pedestrian': 'pedestrian.moving',
                'motorcycle': 'cycle.without_rider',
                'bicycle': 'vehicle.moving',
            }
            attr_id = nus_attributes.index(DefaultAttribute[ann_rec['attribute_name']])
            repro_rec['attribute_name'] = ann_rec['attribute_name']
            repro_rec['attribute_id'] = attr_id

            repro_recs.append(repro_rec)

    return repro_recs


def post_process_coords(
    corner_coords: List, imsize: Tuple[int, int] = (1600, 900)
) -> Union[Tuple[float, float, float, float], None]:
    """Get the intersection of the convex hull of the reprojected bbox corners
    and the image canvas, return None if no intersection.

    Args:
        corner_coords (list[int]): Corner coordinates of reprojected
            bounding box.
        imsize (tuple[int]): Size of the image canvas.

    Return:
        tuple [float]: Intersection of the convex hull of the 2D box
            corners and the image canvas.
    """
    polygon_from_2d_box = MultiPoint(corner_coords).convex_hull
    img_canvas = box(0, 0, imsize[0], imsize[1])

    if polygon_from_2d_box.intersects(img_canvas):
        img_intersection = polygon_from_2d_box.intersection(img_canvas)
        intersection_coords = np.array(
            [coord for coord in img_intersection.exterior.coords])

        min_x = min(intersection_coords[:, 0])
        min_y = min(intersection_coords[:, 1])
        max_x = max(intersection_coords[:, 0])
        max_y = max(intersection_coords[:, 1])

        return min_x, min_y, max_x, max_y
    else:
        return None


def generate_record(ann_rec: dict, x1: float, y1: float, x2: float, y2: float,
                    sample_data_token: str, filename: str) -> OrderedDict:
    """Generate one 2D annotation record given various information on top of
    the 2D bounding box coordinates.

    Args:
        ann_rec (dict): Original 3d annotation record.
        x1 (float): Minimum value of the x coordinate.
        y1 (float): Minimum value of the y coordinate.
        x2 (float): Maximum value of the x coordinate.
        y2 (float): Maximum value of the y coordinate.
        sample_data_token (str): Sample data token.
        filename (str):The corresponding image file where the annotation
            is present.

    Returns:
        dict: A sample 2D annotation record.
            - file_name (str): file name
            - image_id (str): sample data token
            - area (float): 2d box area
            - category_name (str): category name
            - category_id (int): category id
            - bbox (list[float]): left x, top y, dx, dy of 2d box
            - iscrowd (int): whether the area is crowd
    """
    repro_rec = OrderedDict()
    repro_rec['sample_data_token'] = sample_data_token
    coco_rec = dict()

    relevant_keys = [
        'attribute_tokens',
        'category_name',
        'instance_token',
        'next',
        'num_lidar_pts',
        'num_radar_pts',
        'prev',
        'sample_annotation_token',
        'sample_data_token',
        'visibility_token',
    ]

    for key, value in ann_rec.items():
        if key in relevant_keys:
            repro_rec[key] = value

    repro_rec['bbox_corners'] = [x1, y1, x2, y2]
    repro_rec['filename'] = filename

    coco_rec['file_name'] = filename
    coco_rec['image_id'] = sample_data_token
    coco_rec['area'] = (y2 - y1) * (x2 - x1)

    if repro_rec['category_name'] not in NuScenesDataset.NameMapping:
        return None
    cat_name = NuScenesDataset.NameMapping[repro_rec['category_name']]
    coco_rec['category_name'] = cat_name
    coco_rec['category_id'] = nus_categories.index(cat_name)
    coco_rec['bbox'] = [x1, y1, x2 - x1, y2 - y1]
    coco_rec['iscrowd'] = 0

    return coco_rec
