
import os
import csv
import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.spatial import ConvexHull
from copy import deepcopy
import sys
import math
from ._base_dataset import _BaseDataset
from .. import utils
from ..utils import TrackEvalException
from .. import _timing


class Kitti3DBox(_BaseDataset):
    """Dataset class for KITTI 3D bounding box tracking"""

    @staticmethod
    def get_default_dataset_config():
        """Default class config values"""
        code_path = utils.get_code_path()
        default_config = {
            # Location of GT data
            'GT_FOLDER': os.path.join(code_path, 'data/gt/kitti/kitti_2d_box_train'),
            # Trackers location
            'TRACKERS_FOLDER': os.path.join(code_path, 'data/trackers/kitti/kitti_2d_box_train/'),
            # Where to save eval results (if None, same as TRACKERS_FOLDER)
            'OUTPUT_FOLDER': None,
            # Filenames of trackers to eval (if None, all in folder)
            'TRACKERS_TO_EVAL': None,
            # Valid: ['car', 'pedestrian']
            'CLASSES_TO_EVAL': ['car', 'pedestrian'],
            'SPLIT_TO_EVAL': 'training',  # Valid: 'training', 'val', 'training_minus_val', 'test'
            'INPUT_AS_ZIP': False,  # Whether tracker input files are zipped
            'PRINT_CONFIG': True,  # Whether to print current config
            # Tracker files are in TRACKER_FOLDER/tracker_name/TRACKER_SUB_FOLDER
            'TRACKER_SUB_FOLDER': 'data',
            # Output files are saved in OUTPUT_FOLDER/tracker_name/OUTPUT_SUB_FOLDER
            'OUTPUT_SUB_FOLDER': '',
            # Names of trackers to display, if None: TRACKERS_TO_EVAL
            'TRACKER_DISPLAY_NAMES': None,
        }
        return default_config

    def __init__(self, config=None):
        """Initialise dataset, checking that all required files are present"""
        super().__init__()
        # Fill non-given config values with defaults
        self.config = utils.init_config(
            config, self.get_default_dataset_config(), self.get_name())
        self.gt_fol = self.config['GT_FOLDER']
        self.tracker_fol = self.config['TRACKERS_FOLDER']
        self.should_classes_combine = False
        self.data_is_zipped = self.config['INPUT_AS_ZIP']

        self.output_fol = self.config['OUTPUT_FOLDER']
        if self.output_fol is None:
            self.output_fol = self.tracker_fol

        self.tracker_sub_fol = self.config['TRACKER_SUB_FOLDER']
        self.output_sub_fol = self.config['OUTPUT_SUB_FOLDER']

        self.max_occlusion = 2
        self.max_truncation = 0
        self.min_height = 25

        # Get classes to eval
        self.valid_classes = ['car', 'pedestrian']
        self.class_list = [cls.lower() if cls.lower() in self.valid_classes else None
                           for cls in self.config['CLASSES_TO_EVAL']]
        if not all(self.class_list):
            raise TrackEvalException(
                'Attempted to evaluate an invalid class. Only classes [car, pedestrian] are valid.')
        self.class_name_to_class_id = {'car': 1, 'van': 2, 'truck': 3, 'pedestrian': 4, 'person': 5,  # person sitting
                                       'cyclist': 6, 'tram': 7, 'misc': 8, 'dontcare': 9, 'car_2': 1}

        # Get sequences to eval and check gt files exist
        self.seq_list = []
        self.seq_lengths = {}
        seqmap_name = 'evaluate_tracking.seqmap.' + \
            self.config['SPLIT_TO_EVAL']
        seqmap_file = os.path.join(self.gt_fol, seqmap_name)
        if not os.path.isfile(seqmap_file):
            raise TrackEvalException(
                'no seqmap found: ' + os.path.basename(seqmap_file))
        with open(seqmap_file) as fp:
            dialect = csv.Sniffer().sniff(fp.read(1024))
            fp.seek(0)
            reader = csv.reader(fp, dialect)
            for row in reader:
                if len(row) >= 4:
                    seq = row[0]
                    self.seq_list.append(seq)
                    self.seq_lengths[seq] = int(row[3])
                    if not self.data_is_zipped:
                        curr_file = os.path.join(
                            self.gt_fol, 'label_02', seq + '.txt')
                        if not os.path.isfile(curr_file):
                            raise TrackEvalException(
                                'GT file not found: ' + os.path.basename(curr_file))
            if self.data_is_zipped:
                curr_file = os.path.join(self.gt_fol, 'data.zip')
                if not os.path.isfile(curr_file):
                    raise TrackEvalException(
                        'GT file not found: ' + os.path.basename(curr_file))

        # Get trackers to eval
        if self.config['TRACKERS_TO_EVAL'] is None:
            self.tracker_list = os.listdir(self.tracker_fol)
        else:
            self.tracker_list = self.config['TRACKERS_TO_EVAL']

        if self.config['TRACKER_DISPLAY_NAMES'] is None:
            self.tracker_to_disp = dict(
                zip(self.tracker_list, self.tracker_list))
        elif (self.config['TRACKERS_TO_EVAL'] is not None) and (
                len(self.config['TRACKER_DISPLAY_NAMES']) == len(self.tracker_list)):
            self.tracker_to_disp = dict(
                zip(self.tracker_list, self.config['TRACKER_DISPLAY_NAMES']))
        else:
            raise TrackEvalException(
                'List of tracker files and tracker display names do not match.')

        for tracker in self.tracker_list:
            if self.data_is_zipped:
                curr_file = os.path.join(
                    self.tracker_fol, tracker, self.tracker_sub_fol + '.zip')
                if not os.path.isfile(curr_file):
                    raise TrackEvalException(
                        'Tracker file not found: ' + tracker + '/' + os.path.basename(curr_file))
            else:
                for seq in self.seq_list:
                    curr_file = os.path.join(
                        self.tracker_fol, tracker, self.tracker_sub_fol, seq + '.txt')
                    if not os.path.isfile(curr_file):
                        raise TrackEvalException(
                            'Tracker file not found: ' + tracker + '/' + self.tracker_sub_fol + '/' + os.path.basename(
                                curr_file))

    def get_display_name(self, tracker):
        return self.tracker_to_disp[tracker]

    def _load_raw_file(self, tracker, seq, is_gt):
        """Load a file (gt or tracker) in the kitti 2D box format

        If is_gt, this returns a dict which contains the fields:
        [gt_ids, gt_classes] : list (for each timestep) of 1D NDArrays (for each det).
        [gt_dets, gt_crowd_ignore_regions]: list (for each timestep) of lists of detections.
        [gt_extras] : list (for each timestep) of dicts (for each extra) of 1D NDArrays (for each det).

        if not is_gt, this returns a dict which contains the fields:
        [tracker_ids, tracker_classes, tracker_confidences] : list (for each timestep) of 1D NDArrays (for each det).
        [tracker_dets]: list (for each timestep) of lists of detections.
        """
        # File location
        if self.data_is_zipped:
            if is_gt:
                zip_file = os.path.join(self.gt_fol, 'data.zip')
            else:
                zip_file = os.path.join(
                    self.tracker_fol, tracker, self.tracker_sub_fol + '.zip')
            file = seq + '.txt'
        else:
            zip_file = None
            if is_gt:
                file = os.path.join(self.gt_fol, 'label_02', seq + '.txt')
            else:
                file = os.path.join(self.tracker_fol, tracker,
                                    self.tracker_sub_fol, seq + '.txt')

        # Ignore regions
        if is_gt:
            crowd_ignore_filter = {2: ['dontcare']}
        else:
            crowd_ignore_filter = None

        # Valid classes
        valid_filter = {2: [x for x in self.class_list]}
        if is_gt:
            if 'car' in self.class_list:
                valid_filter[2].append('van')
            if 'pedestrian' in self.class_list:
                valid_filter[2] += ['person']

        # Convert kitti class strings to class ids
        convert_filter = {2: self.class_name_to_class_id}

        # Load raw data from text file
        read_data, ignore_data = self._load_simple_text_file(file, time_col=0, id_col=1, remove_negative_ids=True,
                                                             valid_filter=valid_filter,
                                                             crowd_ignore_filter=crowd_ignore_filter,
                                                             convert_filter=convert_filter,
                                                             is_zipped=self.data_is_zipped, zip_file=zip_file)
        # Convert data to required format
        num_timesteps = self.seq_lengths[seq]
        data_keys = ['ids', 'classes', 'dets']
        if is_gt:
            data_keys += ['gt_crowd_ignore_regions', 'gt_extras']
        else:
            data_keys += ['tracker_confidences']
        raw_data = {key: [None] * num_timesteps for key in data_keys}
        for t in range(num_timesteps):
            time_key = str(t)
            if time_key in read_data.keys():
                time_data = np.asarray(read_data[time_key], dtype=np.float)
                raw_data['dets'][t] = np.atleast_2d(time_data[:, 6:17])
                raw_data['ids'][t] = np.atleast_1d(time_data[:, 1]).astype(int)
                raw_data['classes'][t] = np.atleast_1d(
                    time_data[:, 2]).astype(int)
                if is_gt:
                    raw_data['ids'][t] = np.atleast_1d(time_data[:, 1]).astype(int)
                else:
                    raw_data['ids'][t] = np.atleast_1d(time_data[:, 1]).astype(str)
                if is_gt:
                    gt_extras_dict = {'truncation': np.atleast_1d(time_data[:, 3].astype(int)),
                                      'occlusion': np.atleast_1d(time_data[:, 4].astype(int))}
                    raw_data['gt_extras'][t] = gt_extras_dict
                else:
                    if time_data.shape[1] > 17:
                        raw_data['tracker_confidences'][t] = np.atleast_1d(
                            time_data[:, 17])
                    else:
                        raw_data['tracker_confidences'][t] = np.ones(
                            time_data.shape[0])
            else:
                raw_data['dets'][t] = np.empty((0, 4))
                if is_gt:
                    raw_data['ids][t] = np.empty(0).astype(int)
                else:
                    raw_data['ids'][t] = np.empty(0).astype(str)
                raw_data['classes'][t] = np.empty(0).astype(int)
                if is_gt:
                    gt_extras_dict = {
                        'truncation': np.empty(0),
                        'occlusion': np.empty(0),
                    }
                    raw_data['gt_extras'][t] = gt_extras_dict
                else:
                    raw_data['tracker_confidences'][t] = np.empty(0)
            if is_gt:
                if time_key in ignore_data.keys():
                    time_ignore = np.asarray(ignore_data[time_key], dtype=np.float)
                    raw_data['gt_crowd_ignore_regions'][t] = np.atleast_2d(
                        time_ignore[:, 6:17]
                    )
                else:
                    raw_data['gt_crowd_ignore_regions'][t] = np.empty((0, 4))

        if not is_gt:
            # map possibly non-int tracker ids to int ids
            unique_tracker_ids = set()
            for t in range(num_timesteps):
                unique_tracker_ids.update(set(raw_data['ids'][t]))
            # replace every old id w/ its enumerated counterpart
            for new_id, old_id in enumerate(unique_tracker_ids):
                for t in range(num_timesteps):
                    raw_data['ids'][t] = np.where(
                        raw_data['ids'][t] == old_id, new_id, raw_data['ids'][t]
                    )
        # recast to int (intermediate float step necessary because e.g. the string `3.0` can't be cast to int)
        for t in range(num_timesteps):
            raw_data['ids'][t] = raw_data['ids'][t].astype(float).astype(int)

        if is_gt:
            key_map = {'ids': 'gt_ids',
                       'classes': 'gt_classes',
                       'dets': 'gt_dets'}
        else:
            key_map = {'ids': 'tracker_ids',
                       'classes': 'tracker_classes',
                       'dets': 'tracker_dets'}
        for k, v in key_map.items():
            raw_data[v] = raw_data.pop(k)
        raw_data['num_timesteps'] = num_timesteps
        raw_data['seq'] = seq
        return raw_data

    @_timing.time
    def get_preprocessed_seq_data(self, raw_data, cls):
        """ Preprocess data for a single sequence for a single class ready for evaluation.
        Inputs:
             - raw_data is a dict containing the data for the sequence already read in by get_raw_seq_data().
             - cls is the class to be evaluated.
        Outputs:
             - data is a dict containing all of the information that metrics need to perform evaluation.
                It contains the following fields:
                    [num_timesteps, num_gt_ids, num_tracker_ids, num_gt_dets, num_tracker_dets] : integers.
                    [gt_ids, tracker_ids, tracker_confidences]: list (for each timestep) of 1D NDArrays (for each det).
                    [gt_dets, tracker_dets]: list (for each timestep) of lists of detections.
                    [similarity_scores]: list (for each timestep) of 2D NDArrays.
        Notes:
            General preprocessing (preproc) occurs in 4 steps. Some datasets may not use all of these steps.
                1) Extract only detections relevant for the class to be evaluated (including distractor detections).
                2) Match gt dets and tracker dets. Remove tracker dets that are matched to a gt det that is of a
                    distractor class, or otherwise marked as to be removed.
                3) Remove unmatched tracker dets if they fall within a crowd ignore region or don't meet a certain
                    other criteria (e.g. are too small).
                4) Remove gt dets that were only useful for preprocessing and not for actual evaluation.
            After the above preprocessing steps, this function also calculates the number of gt and tracker detections
                and unique track ids. It also relabels gt and tracker ids to be contiguous and checks that ids are
                unique within each timestep.

        KITTI:
            In KITTI, the 4 preproc steps are as follow:
                1) There are two classes (pedestrian and car) which are evaluated separately.
                2) For the pedestrian class, the 'person' class is distractor objects (people sitting).
                    For the car class, the 'van' class are distractor objects.
                    GT boxes marked as having occlusion level > 2 or truncation level > 0 are also treated as
                        distractors.
                3) Crowd ignore regions are used to remove unmatched detections. Also unmatched detections with
                    height <= 25 pixels are removed.
                4) Distractor gt dets (including truncated and occluded) are removed.
        """
        if cls == 'pedestrian':
            distractor_classes = [self.class_name_to_class_id['person']]
        elif cls == 'car':
            distractor_classes = [self.class_name_to_class_id['van']]
        else:
            raise (TrackEvalException('Class %s is not evaluatable' % cls))
        cls_id = self.class_name_to_class_id[cls]

        data_keys = ['gt_ids', 'tracker_ids', 'gt_dets',
                     'tracker_dets', 'tracker_confidences', 'similarity_scores']
        data = {key: [None] * raw_data['num_timesteps'] for key in data_keys}
        unique_gt_ids = []
        unique_tracker_ids = []
        num_gt_dets = 0
        num_tracker_dets = 0
        for t in range(raw_data['num_timesteps']):

            # Only extract relevant dets for this class for preproc and eval (cls + distractor classes)
            gt_class_mask = np.sum([raw_data['gt_classes'][t] == c for c in [
                                   cls_id] + distractor_classes], axis=0)
            gt_class_mask = gt_class_mask.astype(np.bool)
            gt_ids = raw_data['gt_ids'][t][gt_class_mask]
            gt_dets = raw_data['gt_dets'][t][gt_class_mask]
            gt_classes = raw_data['gt_classes'][t][gt_class_mask]
            gt_occlusion = raw_data['gt_extras'][t]['occlusion'][gt_class_mask]
            gt_truncation = raw_data['gt_extras'][t]['truncation'][gt_class_mask]

            tracker_class_mask = np.atleast_1d(
                raw_data['tracker_classes'][t] == cls_id)
            tracker_class_mask = tracker_class_mask.astype(np.bool)
            tracker_ids = raw_data['tracker_ids'][t][tracker_class_mask]
            tracker_dets = raw_data['tracker_dets'][t][tracker_class_mask]
            tracker_confidences = raw_data['tracker_confidences'][t][tracker_class_mask]
            similarity_scores, similarity_scores_2d = raw_data['similarity_scores'][t]
            similarity_scores = similarity_scores[gt_class_mask, :][:, tracker_class_mask]
            similarity_scores_2d = similarity_scores_2d[gt_class_mask, :][:, tracker_class_mask]

            # to_delete_by_confidence = tracker_confidences < 0.7
            # tracker_ids = np.delete(
            #     tracker_ids, to_delete_by_confidence, axis=0)
            # tracker_dets = np.delete(
            #     tracker_dets, to_delete_by_confidence, axis=0)
            # tracker_confidences = np.delete(
            #     tracker_confidences, to_delete_by_confidence, axis=0)
            # similarity_scores = np.delete(
            #     similarity_scores, to_delete_by_confidence, axis=1)
            # similarity_scores_2d = np.delete(
            #     similarity_scores_2d, to_delete_by_confidence, axis=1)

            # Match tracker and gt dets (with hungarian algorithm) and remove tracker dets which match with gt dets
            # which are labeled as truncated, occluded, or belonging to a distractor class.
            to_remove_matched = np.array([], np.int)
            unmatched_indices = np.arange(tracker_ids.shape[0])
            if gt_ids.shape[0] > 0 and tracker_ids.shape[0] > 0:
                matching_scores = similarity_scores_2d.copy()
                matching_scores[matching_scores < 0.25] = 0
                match_rows, match_cols = linear_sum_assignment(
                    -matching_scores)
                actually_matched_mask = matching_scores[match_rows,
                                                        match_cols] > 0
                match_rows = match_rows[actually_matched_mask]
                match_cols = match_cols[actually_matched_mask]

                is_distractor_class = np.isin(
                    gt_classes[match_rows], distractor_classes)
                # print(gt_occlusion[match_rows] > self.max_occlusion)
                # print(gt_truncation[match_rows] > self.max_truncation)
                is_occluded_or_truncated = np.logical_or(gt_occlusion[match_rows] > self.max_occlusion,
                                                         gt_truncation[match_rows] > self.max_truncation)
                to_remove_matched = np.logical_or(
                    is_distractor_class, is_occluded_or_truncated)
                to_remove_matched = match_cols[to_remove_matched]
                unmatched_indices = np.delete(
                    unmatched_indices, match_cols, axis=0)

            # For unmatched tracker dets, also remove those smaller than a minimum height.
            unmatched_tracker_dets = tracker_dets[unmatched_indices, :]
            unmatched_heights = unmatched_tracker_dets[:,
                                                       3] - unmatched_tracker_dets[:, 1]
            is_too_small = unmatched_heights <= self.min_height

            # For unmatched tracker dets, also remove those that are greater than 50% within a crowd ignore region.
            crowd_ignore_regions = raw_data['gt_crowd_ignore_regions'][t]
            intersection_with_ignore_region = self._calculate_box_ious(unmatched_tracker_dets[:, :4], crowd_ignore_regions[:, :4],
                                                                       box_format='x0y0x1y1', do_ioa=True)
            is_within_crowd_ignore_region = np.any(
                intersection_with_ignore_region > 0.5, axis=1)

            # Apply preprocessing to remove all unwanted tracker dets.
            to_remove_unmatched = unmatched_indices[np.logical_or(
                is_too_small, is_within_crowd_ignore_region)]
            to_remove_tracker = np.concatenate(
                (to_remove_matched, to_remove_unmatched), axis=0)
            data['tracker_ids'][t] = np.delete(
                tracker_ids, to_remove_tracker, axis=0)
            data['tracker_dets'][t] = np.delete(
                tracker_dets, to_remove_tracker, axis=0)
            data['tracker_confidences'][t] = np.delete(
                tracker_confidences, to_remove_tracker, axis=0)
            similarity_scores = np.delete(
                similarity_scores, to_remove_tracker, axis=1)

            # Also remove gt dets that were only useful for preprocessing and are not needed for evaluation.
            # These are those that are occluded, truncated and from distractor objects.
            gt_to_keep_mask = (np.less_equal(gt_occlusion, self.max_occlusion)) & \
                              (np.less_equal(gt_truncation, self.max_truncation)) & \
                              (np.equal(gt_classes, cls_id))
            data['gt_ids'][t] = gt_ids[gt_to_keep_mask]
            data['gt_dets'][t] = gt_dets[gt_to_keep_mask, :]
            data['similarity_scores'][t] = similarity_scores[gt_to_keep_mask]

            unique_gt_ids += list(np.unique(data['gt_ids'][t]))
            unique_tracker_ids += list(np.unique(data['tracker_ids'][t]))
            num_tracker_dets += len(data['tracker_ids'][t])
            num_gt_dets += len(data['gt_ids'][t])

        # Re-label IDs such that there are no empty IDs
        if len(unique_gt_ids) > 0:
            unique_gt_ids = np.unique(unique_gt_ids)
            gt_id_map = np.nan * np.ones((np.max(unique_gt_ids) + 1))
            gt_id_map[unique_gt_ids] = np.arange(len(unique_gt_ids))
            for t in range(raw_data['num_timesteps']):
                if len(data['gt_ids'][t]) > 0:
                    data['gt_ids'][t] = gt_id_map[data['gt_ids']
                                                  [t]].astype(np.int)
        if len(unique_tracker_ids) > 0:
            unique_tracker_ids = np.unique(unique_tracker_ids)
            tracker_id_map = np.nan * np.ones((np.max(unique_tracker_ids) + 1))
            tracker_id_map[unique_tracker_ids] = np.arange(
                len(unique_tracker_ids))
            for t in range(raw_data['num_timesteps']):
                if len(data['tracker_ids'][t]) > 0:
                    data['tracker_ids'][t] = tracker_id_map[data['tracker_ids'][t]].astype(
                        np.int)

        # Record overview statistics.
        data['num_tracker_dets'] = num_tracker_dets
        data['num_gt_dets'] = num_gt_dets
        data['num_tracker_ids'] = len(unique_tracker_ids)
        data['num_gt_ids'] = len(unique_gt_ids)
        data['num_timesteps'] = raw_data['num_timesteps']
        data['seq'] = raw_data['seq']

        # Ensure that ids are unique per timestep.
        self._check_unique_ids(data)

        return data

    def _calculate_similarities(self, gt_dets_t, tracker_dets_t):
        # print(gt_dets_t, tracker_dets_t)
        similarity_scores = self.__box_3d_GIoU(
            gt_dets_t, tracker_dets_t, box_format='xyzhwlr')
        # print(similarity_scores)
        similarity_scores_2d = self._calculate_box_ious(
            gt_dets_t[:, 0:4], tracker_dets_t[:, 0:4], box_format='x0y0x1y1')

        return (similarity_scores, similarity_scores_2d)

    def __polygon_clip(self, subjectPolygon, clipPolygon):
        """ Clip a polygon with another polygon.

        Ref: https://rosettacode.org/wiki/Sutherland-Hodgman_polygon_clipping#Python

        Args:
        subjectPolygon: a list of (x,y) 2d points, any polygon.
        clipPolygon: a list of (x,y) 2d points, has to be *convex*
        Note:
        **points have to be counter-clockwise ordered**

        Return:
        a list of (x,y) vertex point for the intersection polygon.
        """
        def inside(p):
            return(cp2[0]-cp1[0])*(p[1]-cp1[1]) > (cp2[1]-cp1[1])*(p[0]-cp1[0])

        def computeIntersection():
            dc = [cp1[0] - cp2[0], cp1[1] - cp2[1]]
            dp = [s[0] - e[0], s[1] - e[1]]
            n1 = cp1[0] * cp2[1] - cp1[1] * cp2[0]
            n2 = s[0] * e[1] - s[1] * e[0]
            n3 = 1.0 / (dc[0] * dp[1] - dc[1] * dp[0])
            return [(n1*dp[0] - n2*dc[0]) * n3, (n1*dp[1] - n2*dc[1]) * n3]

        outputList = subjectPolygon
        cp1 = clipPolygon[-1]

        for clipVertex in clipPolygon:
            cp2 = clipVertex
            inputList = outputList
            outputList = []
            s = inputList[-1]

            for subjectVertex in inputList:
                e = subjectVertex
                if inside(e):
                    if not inside(s):
                        outputList.append(computeIntersection())
                    outputList.append(e)
                elif inside(s):
                    outputList.append(computeIntersection())
                s = e
            cp1 = cp2
            if len(outputList) == 0:
                return None
        return(outputList)

    def __convex_hull_intersection(self, p1, p2):
        """ Compute area of two convex hull's intersection area.
            p1,p2 are a list of (x,y) tuples of hull vertices.
            return a list of (x,y) for the intersection and its volume
        """
        inter_p = self.__polygon_clip(p1, p2)
        if inter_p is not None:
            hull_inter = ConvexHull(inter_p)
            return inter_p, hull_inter.volume
        else:
            return None, 0.0

    def __box3d_vol(self, corners):
        ''' corners: (8,3) no assumption on axis direction '''
        a = np.sqrt(np.sum((corners[0, :] - corners[1, :])**2))
        b = np.sqrt(np.sum((corners[1, :] - corners[2, :])**2))
        c = np.sqrt(np.sum((corners[0, :] - corners[4, :])**2))
        return a*b*c

    def __box3d_iou(self, corners1, corners2):
        ''' Compute 3D bounding box IoU.

        Input:
            corners1: numpy array (8,3), assume up direction is negative Y
            corners2: numpy array (8,3), assume up direction is negative Y
        Output:
            intersection, volume1, volume2: 3D bounding box IoU

        todo (rqi): add more description on corner points' orders.
        '''
        # corner points are in counter clockwise order
        rect1 = [(corners1[i, 0], corners1[i, 2]) for i in range(3, -1, -1)]
        rect2 = [(corners2[i, 0], corners2[i, 2]) for i in range(3, -1, -1)]
        inter, inter_area = self.__convex_hull_intersection(rect1, rect2)

        ymax = min(corners1[0, 1], corners2[0, 1])
        ymin = max(corners1[4, 1], corners2[4, 1])
        inter_vol = inter_area * max(0.0, ymax-ymin)
        vol1 = self.__box3d_vol(corners1)
        vol2 = self.__box3d_vol(corners2)
        return inter_vol, vol1, vol2

    def __roty(self, t):
        ''' Rotation about the y-axis. '''
        c = np.cos(t)
        s = np.sin(t)
        return np.array([[c,  0,  s],
                         [0,  1,  0],
                         [-s, 0,  c]])

    def __compute_box_3d(self, obj):
        ''' Takes an object and a projection matrix (P) and projects the 3d
            bounding box into the image plane.
            Returns:
                corners_2d: (8,2) array in left image coord.
                corners_3d: (8,3) array in in rect camera coord.
        '''
        # compute rotational matrix around yaw axis
        R = self.__roty(obj[10])

        # 3d bounding box dimensions
        l = obj[6]
        w = obj[5]
        h = obj[4]

        # 3d bounding box corners
        x_corners = [l/2, l/2, -l/2, -l/2, l/2, l/2, -l/2, -l/2]
        y_corners = [0, 0, 0, 0, -h, -h, -h, -h]
        z_corners = [w/2, -w/2, -w/2, w/2, w/2, -w/2, -w/2, w/2]

        # rotate and translate 3d bounding box
        corners_3d = np.dot(R, np.vstack([x_corners, y_corners, z_corners]))
        # print corners_3d.shape
        corners_3d[0, :] = corners_3d[0, :] + obj[7]
        corners_3d[1, :] = corners_3d[1, :] + obj[8]
        corners_3d[2, :] = corners_3d[2, :] + obj[9]
        # print('cornsers_3d: ', corners_3d)

        return np.transpose(corners_3d)

    def __bbox3d_min_oobb(self, corners1, corners2):
        rect1 = [[corners1[i, 0], corners1[i, 2]] for i in range(3, -1, -1)]
        rect2 = [[corners2[i, 0], corners2[i, 2]] for i in range(3, -1, -1)]
        vertices = np.array(rect1 + rect2)
        hull = ConvexHull(vertices)
        _, area, _, _, _, _ = self.__min_bounding_rect(vertices[hull.vertices])
        ymax = max(corners1[0, 1], corners2[0, 1])
        ymin = min(corners1[4, 1], corners2[4, 1])
        return area * (ymax - ymin)

    def __box_3d_GIoU(self, bboxes1, bboxes2, do_ioa=False, box_format='xyzhwlr'):
        bboxes1 = deepcopy(bboxes1)
        bboxes2 = deepcopy(bboxes2)

        giou3d_metrics = []
        for aa in bboxes1:
            row_metrics = []
            for bb in bboxes2:
                if box_format != 'xyzhwlr':
                    raise (TrackEvalException(
                        'box_format %s is not implemented' % box_format))

                aa_3d = self.__compute_box_3d(aa)
                bb_3d = self.__compute_box_3d(bb)

                inter, vol1, vol2 = self.__box3d_iou(
                    aa_3d, bb_3d)
                A_c = self.__bbox3d_min_oobb(aa_3d, bb_3d)
                if do_ioa:
                    giou3d = inter / (vol1) - \
                        (A_c - (vol1 + vol2 - inter)) / A_c
                else:
                    giou3d = inter / (vol1 + vol2 - inter) - \
                        (A_c - (vol1 + vol2 - inter)) / A_c
                row_metrics.append((giou3d + 1) / 2.)
            if len(row_metrics):
                giou3d_metrics.append(row_metrics)
        return np.array(giou3d_metrics).reshape((len(bboxes1), len(bboxes2)))

    def __min_bounding_rect(self, hull_points_2d):
        """
        Ref: https://github.com/dbworth/minimum-area-bounding-rectangle/blob/master/python/min_bounding_rect.py
        """
        # print "Input convex hull points: "
        # print hull_points_2d

        # Compute edges (x2-x1,y2-y1)
        edges = np.zeros((len(hull_points_2d)-1, 2))  # empty 2 column array
        for i in range(len(edges)):
            edge_x = hull_points_2d[i+1, 0] - hull_points_2d[i, 0]
            edge_y = hull_points_2d[i+1, 1] - hull_points_2d[i, 1]
            edges[i] = [edge_x, edge_y]
        # print "Edges: \n", edges

        # Calculate edge angles   atan2(y/x)
        edge_angles = np.zeros((len(edges)))  # empty 1 column array
        for i in range(len(edge_angles)):
            edge_angles[i] = math.atan2(edges[i, 1], edges[i, 0])
        # print "Edge angles: \n", edge_angles

        # Check for angles in 1st quadrant
        for i in range(len(edge_angles)):
            # want strictly positive answers
            edge_angles[i] = abs(edge_angles[i] % (math.pi/2))
        # print "Edge angles in 1st Quadrant: \n", edge_angles

        # Remove duplicate angles
        edge_angles = np.unique(edge_angles)
        # print "Unique edge angles: \n", edge_angles

        # Test each angle to find bounding box with smallest area
        # rot_angle, area, width, height, min_x, max_x, min_y, max_y
        min_bbox = (0, sys.maxsize, 0, 0, 0, 0, 0, 0)
        # print "Testing", len(edge_angles), "possible rotations for bounding box... \n"
        for i in range(len(edge_angles)):

            # Create rotation matrix to shift points to baseline
            # R = [ cos(theta)      , cos(theta-PI/2)
            #       cos(theta+PI/2) , cos(theta)     ]
            R = np.array([[math.cos(edge_angles[i]), math.cos(edge_angles[i]-(math.pi/2))],
                          [math.cos(edge_angles[i]+(math.pi/2)), math.cos(edge_angles[i])]])
            # print "Rotation matrix for ", edge_angles[i], " is \n", R

            # Apply this rotation to convex hull points
            rot_points = np.dot(R, np.transpose(hull_points_2d))  # 2x2 * 2xn
            # print "Rotated hull points are \n", rot_points

            # Find min/max x,y points
            min_x = np.nanmin(rot_points[0], axis=0)
            max_x = np.nanmax(rot_points[0], axis=0)
            min_y = np.nanmin(rot_points[1], axis=0)
            max_y = np.nanmax(rot_points[1], axis=0)
            # print "Min x:", min_x, " Max x: ", max_x, "   Min y:", min_y, " Max y: ", max_y

            # Calculate height/width/area of this bounding rectangle
            width = max_x - min_x
            height = max_y - min_y
            area = width*height
            # print "Potential bounding box ", i, ":  width: ", width, " height: ", height, "  area: ", area

            # Store the smallest rect found first (a simple convex hull might have 2 answers with same area)
            if (area < min_bbox[1]):
                min_bbox = (edge_angles[i], area, width,
                            height, min_x, max_x, min_y, max_y)
            # Bypass, return the last found rect
            #min_bbox = ( edge_angles[i], area, width, height, min_x, max_x, min_y, max_y )

        # Re-create rotation matrix for smallest rect
        angle = min_bbox[0]
        R = np.array([[math.cos(angle), math.cos(angle-(math.pi/2))],
                      [math.cos(angle+(math.pi/2)), math.cos(angle)]])
        # print "Projection matrix: \n", R

        # Project convex hull points onto rotated frame
        proj_points = np.dot(R, np.transpose(hull_points_2d))  # 2x2 * 2xn
        # print "Project hull points are \n", proj_points

        # min/max x,y points are against baseline
        min_x = min_bbox[4]
        max_x = min_bbox[5]
        min_y = min_bbox[6]
        max_y = min_bbox[7]
        # print "Min x:", min_x, " Max x: ", max_x, "   Min y:", min_y, " Max y: ", max_y

        # Calculate center point and project onto rotated frame
        center_x = (min_x + max_x)/2
        center_y = (min_y + max_y)/2
        center_point = np.dot([center_x, center_y], R)
        # print "Bounding box center point: \n", center_point

        # Calculate corner points and project onto rotated frame
        corner_points = np.zeros((4, 2))  # empty 2 column array
        corner_points[0] = np.dot([max_x, min_y], R)
        corner_points[1] = np.dot([min_x, min_y], R)
        corner_points[2] = np.dot([min_x, max_y], R)
        corner_points[3] = np.dot([max_x, max_y], R)
        # print "Bounding box corner points: \n", corner_points

        # print "Angle of rotation: ", angle, "rad  ", angle * (180/math.pi), "deg"

        # rot_angle, area, width, height, center_point, corner_points
        return (angle, min_bbox[1], min_bbox[2], min_bbox[3], center_point, corner_points)
