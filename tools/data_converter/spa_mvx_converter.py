# Copyright (c) OpenMMLab. All rights reserved.
from collections import OrderedDict
from pathlib import Path

import mmcv
import numpy as np
from nuscenes.utils.geometry_utils import view_points

from mmdet3d.core.bbox import box_np_ops_spa_mvx, points_cam2img
from .spa_mvx_data_utils import WaymoInfoGatherer, get_spa_image_info
from .nuscenes_converter import post_process_coords

spa_mvx_categories = ('Pedestrian', 'Cyclist', 'Car', 'Motorcyclist')


def convert_to_spa_mvx_info_version2(info):
    """convert spa_mvx info v1 to v2 if possible.

    Args:
        info (dict): Info of the input spa_mvx data.
            - image (dict): image info
            - calib (dict): calibration info
            - point_cloud (dict): point cloud info
    """
    if 'image' not in info or 'calib' not in info or 'point_cloud' not in info:
        info['image'] = {
            'image_shape': info['img_shape'],
            'image_idx': info['image_idx'],
            'image_path': info['img_path'],
        }
        info['calib'] = {
            'R0_rect': info['calib/R0_rect'],
            'Tr_velo_to_cam': info['calib/Tr_velo_to_cam'],
            'P2': info['calib/P2'],
        }
        info['point_cloud'] = {
            'velodyne_path': info['velodyne_path'],
        }


def _read_imageset_file(path):
    with open(path, 'r') as f:
        lines = f.readlines()
    return [line.splitlines()[0] for line in lines]


class _NumPointsInGTCalculater:
    """Calculate the number of points inside the ground truth box. This is the
    parallel version. For the serialized version, please refer to
    `_calculate_num_points_in_gt`.

    Args:
        data_path (str): Path of the data.
        relative_path (bool): Whether to use relative path.
        remove_outside (bool, optional): Whether to remove points which are
            outside of image. Default: True.
        num_features (int, optional): Number of features per point.
            Default: False.
        num_worker (int, optional): the number of parallel workers to use.
            Default: 8.
    """

    def __init__(self,
                 data_path,
                 relative_path,
                 remove_outside=True,
                 num_features=4,
                 num_worker=8) -> None:
        self.data_path = data_path
        self.relative_path = relative_path
        self.remove_outside = remove_outside
        self.num_features = num_features
        self.num_worker = num_worker

    def calculate_single(self, info):
        pc_info = info['point_cloud']
        image_info = info['image']
        calib = info['calib']
        if self.relative_path:
            v_path = str(Path(self.data_path) / pc_info['velodyne_path'])
        else:
            v_path = pc_info['velodyne_path']
        points_v = np.fromfile(
            v_path, dtype=np.float32,
            count=-1).reshape([-1, self.num_features])
        rect = calib['R0_rect']
        Trv2c = calib['Tr_velo_to_cam']
        P2 = calib['P2']
        if self.remove_outside:
            points_v = box_np_ops_spa_mvx.remove_outside_points(
                points_v, rect, Trv2c, P2, image_info['image_shape'])
        annos = info['annos']
        num_obj = len([n for n in annos['name'] if n != 'DontCare'])
        dims = annos['dimensions'][:num_obj]
        loc = annos['location'][:num_obj]
        rots = annos['rotation_y'][:num_obj]
        gt_boxes_camera = np.concatenate([loc, dims, rots[..., np.newaxis]],
                                         axis=1)
        gt_boxes_lidar = box_np_ops_spa_mvx.box_camera_to_lidar(
            gt_boxes_camera, rect, Trv2c)
        indices = box_np_ops_spa_mvx.points_in_rbbox(points_v[:, :3], gt_boxes_lidar)
        num_points_in_gt = indices.sum(0)
        num_ignored = len(annos['dimensions']) - num_obj
        num_points_in_gt = np.concatenate(
            [num_points_in_gt, -np.ones([num_ignored])])
        annos['num_points_in_gt'] = num_points_in_gt.astype(np.int32)
        return info

    def calculate(self, infos):
        ret_infos = mmcv.track_parallel_progress(self.calculate_single, infos,
                                                 self.num_worker)
        for i, ret_info in enumerate(ret_infos):
            infos[i] = ret_info


def _calculate_num_points_in_gt(data_path,
                                infos,
                                relative_path,
                                remove_outside=True,
                                num_features=4):
    for info in mmcv.track_iter_progress(infos):
        pc_info = info['point_cloud']
        image_info = info['image']
        calib = info['calib']
        if relative_path:
            # v_path = str(Path(data_path) / pc_info['velodyne_path'])
            v_path = pc_info['velodyne_path']
        else:
            v_path = pc_info['velodyne_path']
        points_v = np.fromfile(
            v_path, dtype=np.float32, count=-1).reshape([-1, num_features])
        rect = calib['R0_rect']
        Trv2c = calib['Tr_velo_to_cam']

        P_list = [calib['P0'], calib['P1'], calib['P2'], calib['P3'], calib['P4']]
        Tr_list = [calib['Tr_0'], calib['Tr_1'], calib['Tr_2'], calib['Tr_3'], calib['Tr_4']]
        if remove_outside:
            P = P_list[int(image_info['image_path'].split("/")[5])-1]
            points_v = box_np_ops_spa_mvx.remove_outside_points(
                points_v, rect, Trv2c, P, image_info['image_shape']) #ori

        # points_v = points_v[points_v[:, 0] > 0]
        annos = info['annos']
        num_obj = len([n for n in annos['name'] if n != 'DontCare'])
        # annos = spa_mvx.filter_spa_mvx_anno(annos, ['DontCare'])
        dims = annos['dimensions'][:num_obj]
        loc = annos['location'][:num_obj]
        rots = annos['rotation_y'][:num_obj]

        x_ = loc[:, 2].reshape(-1,1)
        y_ = -loc[:, 0].reshape(-1,1)
        z_ = -loc[:, 1].reshape(-1,1)
        loc = np.concatenate([x_,y_,z_], 1)
        dims = dims[:, [2, 1, 0]] # hwl ==> dxdydz(lwh)
        rots_ = -rots+np.pi/2

        gt_boxes_lidar = np.concatenate([loc, dims, rots_[..., np.newaxis]],
                                         axis=1)


        # gt_boxes_camera = np.concatenate([loc, dims, rots[..., np.newaxis]],
        #                                  axis=1)
        # gt_boxes_lidar = box_np_ops_spa_mvx_mvx.box_camera_to_lidar_0220(
        #     gt_boxes_camera, rect, Trv2c, P)

        indices = box_np_ops_spa_mvx.points_in_rbbox(points_v[:, :3], gt_boxes_lidar)

        num_points_in_gt = indices.sum(0)
        num_ignored = len(annos['dimensions']) - num_obj
        num_points_in_gt = np.concatenate(
            [num_points_in_gt, -np.ones([num_ignored])])
        annos['num_points_in_gt'] = num_points_in_gt.astype(np.int32)


def create_spa_mvx_info_file(data_path,
                           pkl_prefix='spa_mvx',
                           with_plane=False,
                           save_path=None,
                           relative_path=True):
    """Create info file of spa_mvx dataset.

    Given the raw data, generate its related info file in pkl format.

    Args:
        data_path (str): Path of the data root.
        pkl_prefix (str, optional): Prefix of the info file to be generated.
            Default: 'spa_mvx'.
        with_plane (bool, optional): Whether to use plane information.
            Default: False.
        save_path (str, optional): Path to save the info file.
            Default: None.
        relative_path (bool, optional): Whether to use relative path.
            Default: True.
    """
    imageset_folder = Path(data_path) / 'ImageSets'
    train_img_ids = _read_imageset_file(str(imageset_folder / 'train.txt'))
    val_img_ids = _read_imageset_file(str(imageset_folder / 'val.txt'))
    test_img_ids = _read_imageset_file(str(imageset_folder / 'test.txt'))

    print('Generate info. this may take several minutes.')
    if save_path is None:
        save_path = Path(data_path)
    else:
        save_path = Path(save_path)

    spa_mvx_infos_train = get_spa_image_info(
        data_path,
        training=True,
        velodyne=True,
        calib=True,
        with_plane=with_plane,
        image_ids=train_img_ids,
        relative_path=relative_path)
    _calculate_num_points_in_gt(data_path, spa_mvx_infos_train, relative_path=False)
    filename = save_path / f'{pkl_prefix}_infos_train.pkl'
    print(f'spa_mvx info train file is saved to {filename}')
    mmcv.dump(spa_mvx_infos_train, filename)

    spa_mvx_infos_val = get_spa_image_info(
        data_path,
        training=True,
        velodyne=True,
        calib=True,
        with_plane=with_plane,
        image_ids=val_img_ids,
        relative_path=relative_path)
    _calculate_num_points_in_gt(data_path, spa_mvx_infos_val, relative_path)
    filename = save_path / f'{pkl_prefix}_infos_val.pkl'
    print(f'spa_mvx info val file is saved to {filename}')
    mmcv.dump(spa_mvx_infos_val, filename)
    filename = save_path / f'{pkl_prefix}_infos_trainval.pkl'
    print(f'spa_mvx info trainval file is saved to {filename}')
    mmcv.dump(spa_mvx_infos_train + spa_mvx_infos_val, filename)

    spa_mvx_infos_test = get_spa_image_info(
        data_path,
        training=False,
        label_info=False,
        velodyne=True,
        calib=True,
        with_plane=False,
        image_ids=test_img_ids,
        relative_path=relative_path)
    filename = save_path / f'{pkl_prefix}_infos_test.pkl'
    print(f'spa_mvx info test file is saved to {filename}')
    mmcv.dump(spa_mvx_infos_test, filename)


def create_waymo_info_file(data_path,
                           pkl_prefix='waymo',
                           save_path=None,
                           relative_path=True,
                           max_sweeps=5,
                           workers=8):
    """Create info file of waymo dataset.

    Given the raw data, generate its related info file in pkl format.

    Args:
        data_path (str): Path of the data root.
        pkl_prefix (str, optional): Prefix of the info file to be generated.
            Default: 'waymo'.
        save_path (str, optional): Path to save the info file.
            Default: None.
        relative_path (bool, optional): Whether to use relative path.
            Default: True.
        max_sweeps (int, optional): Max sweeps before the detection frame
            to be used. Default: 5.
    """
    imageset_folder = Path(data_path) / 'ImageSets'
    train_img_ids = _read_imageset_file(str(imageset_folder / 'train.txt'))
    val_img_ids = _read_imageset_file(str(imageset_folder / 'val.txt'))
    test_img_ids = _read_imageset_file(str(imageset_folder / 'test.txt'))

    print('Generate info. this may take several minutes.')
    if save_path is None:
        save_path = Path(data_path)
    else:
        save_path = Path(save_path)
    waymo_infos_gatherer_trainval = WaymoInfoGatherer(
        data_path,
        training=True,
        velodyne=True,
        calib=True,
        pose=True,
        relative_path=relative_path,
        max_sweeps=max_sweeps,
        num_worker=workers)
    waymo_infos_gatherer_test = WaymoInfoGatherer(
        data_path,
        training=False,
        label_info=False,
        velodyne=True,
        calib=True,
        pose=True,
        relative_path=relative_path,
        max_sweeps=max_sweeps,
        num_worker=workers)
    num_points_in_gt_calculater = _NumPointsInGTCalculater(
        data_path,
        relative_path,
        num_features=6,
        remove_outside=False,
        num_worker=workers)

    waymo_infos_train = waymo_infos_gatherer_trainval.gather(train_img_ids)
    num_points_in_gt_calculater.calculate(waymo_infos_train)
    filename = save_path / f'{pkl_prefix}_infos_train.pkl'
    print(f'Waymo info train file is saved to {filename}')
    mmcv.dump(waymo_infos_train, filename)
    waymo_infos_val = waymo_infos_gatherer_trainval.gather(val_img_ids)
    num_points_in_gt_calculater.calculate(waymo_infos_val)
    filename = save_path / f'{pkl_prefix}_infos_val.pkl'
    print(f'Waymo info val file is saved to {filename}')
    mmcv.dump(waymo_infos_val, filename)
    filename = save_path / f'{pkl_prefix}_infos_trainval.pkl'
    print(f'Waymo info trainval file is saved to {filename}')
    mmcv.dump(waymo_infos_train + waymo_infos_val, filename)
    waymo_infos_test = waymo_infos_gatherer_test.gather(test_img_ids)
    filename = save_path / f'{pkl_prefix}_infos_test.pkl'
    print(f'Waymo info test file is saved to {filename}')
    mmcv.dump(waymo_infos_test, filename)


def _create_reduced_point_cloud(data_path,
                                info_path,
                                save_path=None,
                                back=False,
                                num_features=4,
                                front_camera_id=2):
    """Create reduced point clouds for given info.

    Args:
        data_path (str): Path of original data.
        info_path (str): Path of data info.
        save_path (str, optional): Path to save reduced point cloud
            data. Default: None.
        back (bool, optional): Whether to flip the points to back.
            Default: False.
        num_features (int, optional): Number of point features. Default: 4.
        front_camera_id (int, optional): The referenced/front camera ID.
            Default: 2.
    """
    spa_mvx_infos = mmcv.load(info_path)

    for info in mmcv.track_iter_progress(spa_mvx_infos):
        pc_info = info['point_cloud']
        image_info = info['image']
        calib = info['calib']

        v_path = pc_info['velodyne_path']
        # v_path = Path(data_path) / v_path
        v_path = Path(v_path)
        points_v = np.fromfile(
            str(v_path), dtype=np.float32,
            count=-1).reshape([-1, num_features])
        rect = calib['R0_rect']
        cam_num = int(image_info['image_path'].split("/")[5]) -1
        P = calib['P{}'.format(cam_num)]

        Trv2c = calib['Tr_velo_to_cam']
        # first remove z < 0 points
        # keep = points_v[:, -1] > 0
        # points_v = points_v[keep]
        # then remove outside.
        if back:
            points_v[:, 0] = -points_v[:, 0]
        
        points_v = box_np_ops_spa_mvx.remove_outside_points(points_v, rect, Trv2c, P, image_info['image_shape'])


        if save_path is None:
            save_dir = v_path.parent.parent / (v_path.parent.stem + '_reduced')
            if not save_dir.exists():
                save_dir.mkdir()
            save_filename = save_dir / v_path.name
            # save_filename = str(v_path) + '_reduced'
            if back:
                save_filename += '_back'
        else:
            save_filename = str(Path(save_path) / v_path.name)
            if back:
                save_filename += '_back'
        with open(save_filename, 'w') as f:
            points_v.tofile(f)


def create_reduced_point_cloud(data_path,
                               pkl_prefix,
                               train_info_path=None,
                               val_info_path=None,
                               test_info_path=None,
                               save_path=None,
                               with_back=False):
    """Create reduced point clouds for training/validation/testing.

    Args:
        data_path (str): Path of original data.
        pkl_prefix (str): Prefix of info files.
        train_info_path (str, optional): Path of training set info.
            Default: None.
        val_info_path (str, optional): Path of validation set info.
            Default: None.
        test_info_path (str, optional): Path of test set info.
            Default: None.
        save_path (str, optional): Path to save reduced point cloud data.
            Default: None.
        with_back (bool, optional): Whether to flip the points to back.
            Default: False.
    """
    if train_info_path is None:
        train_info_path = Path(data_path) / f'{pkl_prefix}_infos_train.pkl'
    if val_info_path is None:
        val_info_path = Path(data_path) / f'{pkl_prefix}_infos_val.pkl'
    if test_info_path is None:
        test_info_path = Path(data_path) / f'{pkl_prefix}_infos_test.pkl'

    print('create reduced point cloud for training set')
    _create_reduced_point_cloud(data_path, train_info_path, save_path)
    print('create reduced point cloud for validation set')
    _create_reduced_point_cloud(data_path, val_info_path, save_path)
    print('create reduced point cloud for testing set')
    _create_reduced_point_cloud(data_path, test_info_path, save_path)
    if with_back:
        _create_reduced_point_cloud(
            data_path, train_info_path, save_path, back=True)
        _create_reduced_point_cloud(
            data_path, val_info_path, save_path, back=True)
        _create_reduced_point_cloud(
            data_path, test_info_path, save_path, back=True)


def export_2d_annotation(root_path, info_path, mono3d=True):
    """Export 2d annotation from the info file and raw data.

    Args:
        root_path (str): Root path of the raw data.
        info_path (str): Path of the info file.
        mono3d (bool, optional): Whether to export mono3d annotation.
            Default: True.
    """
    # get bbox annotations for camera
    spa_mvx_infos = mmcv.load(info_path)
    cat2Ids = [
        dict(id=spa_mvx_categories.index(cat_name), name=cat_name)
        for cat_name in spa_mvx_categories
    ]
    coco_ann_id = 0
    coco_2d_dict = dict(annotations=[], images=[], categories=cat2Ids)
    from os import path as osp
    for info in mmcv.track_iter_progress(spa_mvx_infos):
        coco_infos = get_2d_boxes(info, occluded=[0, 1, 2, 3], mono3d=mono3d)
        (height, width, _) = mmcv.imread(osp.join(info['image']['image_path'])).shape
        coco_2d_dict['images'].append(
            dict(
                file_name=info['image']['image_path'],
                id=info['image']['image_idx'],
                Tri2v=info['calib']['Tr_imu_to_velo'],
                Trv2c=info['calib']['Tr_velo_to_cam'],
                rect=info['calib']['R0_rect'],
                cam_intrinsic=info['calib'],
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


def get_2d_boxes(info, occluded, mono3d=True):
    """Get the 2D annotation records for a given info.

    Args:
        info: Information of the given sample data.
        occluded: Integer (0, 1, 2, 3) indicating occlusion state:
            0 = fully visible, 1 = partly occluded, 2 = largely occluded,
            3 = unknown, -1 = DontCare
        mono3d (bool): Whether to get boxes with mono3d annotation.

    Return:
        list[dict]: List of 2D annotation record that belongs to the input
            `sample_data_token`.
    """
    # Get calibration information

    # P2 = info['calib']['P2']

    repro_recs = []
    # if no annotations in info (test dataset), then return
    if 'annos' not in info:
        return repro_recs

    # Get all the annotation with the specified visibilties.
    ann_dicts = info['annos']
    mask = [(ocld in occluded) for ocld in ann_dicts['occluded']]
    for k in ann_dicts.keys():
        ann_dicts[k] = ann_dicts[k][mask]

    # convert dict of list to list of dict
    ann_recs = []
    for i in range(len(ann_dicts['occluded'])):
        ann_rec = {}
        for k in ann_dicts.keys():
            ann_rec[k] = ann_dicts[k][i]
        ann_recs.append(ann_rec)

    for ann_idx, ann_rec in enumerate(ann_recs):
        # Augment sample_annotation with token information.
        ann_rec['sample_annotation_token'] = \
            f"{info['image']['image_idx']}.{ann_idx}"
        ann_rec['sample_data_token'] = info['image']['image_idx']
        sample_data_token = info['image']['image_idx']

        loc = ann_rec['location'][np.newaxis, :]
        dim = ann_rec['dimensions'][np.newaxis, :]
        rot = ann_rec['rotation_y'][np.newaxis, np.newaxis]
        # transform the center from [0.5, 1.0, 0.5] to [0.5, 0.5, 0.5]
        dst = np.array([0.5, 0.5, 0.5])
        src = np.array([0.5, 1.0, 0.5])
        loc = loc + dim * (dst - src)
        # offset = (info['calib']['P2'][0, 3] - info['calib']['P0'][0, 3]) \
        #     / info['calib']['P2'][0, 0]
        loc_3d = np.copy(loc)
        #loc_3d[0, 0] += offset
        gt_bbox_3d = np.concatenate([loc, dim, rot], axis=1).astype(np.float32)

        # Filter out the corners that are not in front of the calibrated
        # sensor.
        corners_3d = box_np_ops_spa_mvx.center_to_corner_box3d(
            gt_bbox_3d[:, :3],
            gt_bbox_3d[:, 3:6],
            gt_bbox_3d[:, 6], [0.5, 0.5, 0.5],
            axis=1)
        corners_3d = corners_3d[0].T  # (1, 8, 3) -> (3, 8)
        in_front = np.argwhere(corners_3d[2, :] > 0).flatten()
        corners_3d = corners_3d[:, in_front]

        # Project 3d box to 2d.
        #camera_intrinsic = P2
        calib_list = [info['calib']['P0'], info['calib']['P1'], info['calib']['P2'], info['calib']['P3'], info['calib']['P4']]
        
        all_corner_coords = []
        for calib_ in calib_list:
            corner_coords = view_points(corners_3d, calib_,
                                        True).T[:, :2].tolist()
            # all_corner_coords.append(np.dot(calib_[:3, :3], corners_3d).T[:,:2].tolist())
            all_corner_coords.append(corner_coords)

        # Keep only corners that fall within the image.
        final_coords = []
        for cam_corner in all_corner_coords:
            final_coord = post_process_coords(cam_corner)
            final_coords.append(final_coord)

        # Skip if the convex hull of the re-projected corners
        # does not intersect the image canvas.

        for ii, final_coord in enumerate(final_coords):
            if final_coord is None:
                continue
            else:
                min_x, min_y, max_x, max_y = final_coord

            # Generate dictionary record to be included in the .json file.
            repro_rec = generate_record(ann_rec, min_x, min_y, max_x, max_y,
                                        sample_data_token[ii],
                                        info['image']['image_path'][ii])

            # If mono3d=True, add 3D annotations in camera coordinates
            if mono3d and (repro_rec is not None):
                repro_rec['bbox_cam3d'] = np.concatenate(
                    [loc_3d, dim, rot],
                    axis=1).astype(np.float32).squeeze().tolist()
                repro_rec['velo_cam3d'] = -1  # no velocity in spa_mvx

                center3d = np.array(loc).reshape([1, 3])
                center2d = points_cam2img(
                    center3d, calib_list[ii], with_depth=True)
                repro_rec['center2d'] = center2d.squeeze().tolist()
                # normalized center2D + depth
                # samples with depth < 0 will be removed
                if repro_rec['center2d'][2] <= 0:
                    continue

                repro_rec['attribute_name'] = -1  # no attribute in spa_mvx
                repro_rec['attribute_id'] = -1

            repro_recs.append(repro_rec)

    return repro_recs


def generate_record(ann_rec, x1, y1, x2, y2, sample_data_token, filename):
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
            - bbox (list[float]): left x, top y, x_size, y_size of 2d box
            - iscrowd (int): whether the area is crowd
    """
    repro_rec = OrderedDict()
    repro_rec['sample_data_token'] = sample_data_token
    coco_rec = dict()

    key_mapping = {
        'name': 'category_name',
        'num_points_in_gt': 'num_lidar_pts',
        'sample_annotation_token': 'sample_annotation_token',
        'sample_data_token': 'sample_data_token',
    }

    for key, value in ann_rec.items():
        if key in key_mapping.keys():
            repro_rec[key_mapping[key]] = value

    repro_rec['bbox_corners'] = [x1, y1, x2, y2]
    repro_rec['filename'] = filename

    coco_rec['file_name'] = filename
    coco_rec['image_id'] = sample_data_token
    coco_rec['area'] = (y2 - y1) * (x2 - x1)

    if repro_rec['category_name'] not in spa_mvx_categories:
        return None
    cat_name = repro_rec['category_name']
    coco_rec['category_name'] = cat_name
    coco_rec['category_id'] = spa_mvx_categories.index(cat_name)
    coco_rec['bbox'] = [x1, y1, x2 - x1, y2 - y1]
    coco_rec['iscrowd'] = 0

    return coco_rec
