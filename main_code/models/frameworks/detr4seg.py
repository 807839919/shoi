import imghdr
import random
import time
import warnings
from turtle import shape

import cv2
import matplotlib.pyplot as plt
import mmcv
import numpy as np
import torch
import torch.nn.functional as F
from detectron2.utils.visualizer import VisImage, Visualizer
from mmdet.core import bbox2result
from mmdet.datasets.coco_panoptic import INSTANCE_OFFSET
from mmdet.models import DETECTORS, SingleStageDetector

from openpsg.models.relation_heads.approaches import Result
from openpsg.utils.utils import adjust_text_color, draw_text, get_colormap


def seg2Result(segs):
    bboxes, labels, pan_seg = segs
    if isinstance(bboxes, torch.Tensor):
        pan_seg = pan_seg.detach().cpu().numpy()
        labels = labels.detach().cpu().numpy()
        bboxes = bboxes.detach().cpu().numpy()
    # return dict(pan_results=pan_seg)
    return Result(
        refine_bboxes=bboxes,
        labels=labels,
        formatted_masks=dict(pan_results=pan_seg),
        pan_results=pan_seg,
    )


@DETECTORS.register_module()
class DETR4seg(SingleStageDetector):
    def __init__(self,
                 backbone,
                 bbox_head,
                 train_cfg=None,
                 test_cfg=None,
                 pretrained=None,
                 init_cfg=None):
        super(DETR4seg, self).__init__(backbone, None, bbox_head, train_cfg,
                                       test_cfg, pretrained, init_cfg)
        self.obc = self.bbox_head.CLASSES
        self.num_classes = len(self.obc)
        print(self.num_classes)

    def forward_dummy(self, img):

        warnings.warn('Warning! MultiheadAttention in DETR does not '
                      'support flops computation! Do not use the '
                      'results in your papers!')

        batch_size, _, height, width = img.shape
        dummy_img_metas = [
            dict(batch_input_shape=(height, width),
                 img_shape=(height, width, 3)) for _ in range(batch_size)
        ]
        x = self.extract_feat(img)
        outs = self.bbox_head(x, dummy_img_metas)
        return outs

    def forward_train(self,
                      img,
                      img_metas,
                      gt_bboxes,
                      gt_labels,
                      gt_masks,
                      gt_bboxes_ignore=None):

        super(SingleStageDetector, self).forward_train(img, img_metas)

        x = self.extract_feat(img)

        BS, C, H, W = img.shape
        new_gt_masks = []
        for each in gt_masks:
            mask = torch.tensor(each.resize(
                (H // 2, W // 2), interpolation='bilinear').to_ndarray(),
                                device=x[0].device)
            _, h, w = mask.shape
            # padding = (
            #     0,W-w,
            #     0,H-h
            # )
            # mask = F.pad(mask,padding)
            new_gt_masks.append(mask)

        gt_masks = new_gt_masks

        losses = self.bbox_head.forward_train(x, img_metas, gt_bboxes,
                                              gt_labels, gt_masks,
                                              gt_bboxes_ignore)
        return losses

    def simple_test(self, img, img_metas, rescale=False):

        feat = self.extract_feat(img)
        results_list = self.bbox_head.simple_test(feat,
                                                  img_metas,
                                                  rescale=rescale)
        bbox_results = [seg2Result(segs) for segs in results_list]
        return bbox_results


    def aug_test(self, imgs, img_metas, rescale=False):

        assert hasattr(self.bbox_head, 'aug_test'), \
            f'{self.bbox_head.__class__.__name__}' \
            ' does not support test-time augmentation'

        feats = self.extract_feats(imgs)
        results_list = self.bbox_head.aug_test(feats,
                                               img_metas,
                                               rescale=rescale)
        sg_results = [seg2Result(triplets) for triplets in results_list]
        return sg_results

    def onnx_export(self, img, img_metas):

        x = self.extract_feat(img)
        # forward of this head requires img_metas
        outs = self.bbox_head.forward_onnx(x, img_metas)
        # get shape as tensor
        img_shape = torch._shape_as_tensor(img)[2:]
        img_metas[0]['img_shape_for_onnx'] = img_shape

        det_bboxes, det_labels = self.bbox_head.onnx_export(*outs, img_metas)

        return det_bboxes, det_labels

    def show_result(
        self,
        img,
        result,
        score_thr=0.3,
        bbox_color=(72, 101, 241),
        text_color=(72, 101, 241),
        mask_color=None,
        thickness=2,
        font_size=13,
        win_name='',
        show=False,
        wait_time=0,
        out_file=None,
    ):

        # Load image
        img = mmcv.imread(img)
        img = img.copy()  # (H, W, 3)
        img_h, img_w = img.shape[:-1]

        if True:
            # Draw masks
            pan_results = result.pan_results

            ids = np.unique(pan_results)[::-1]
            legal_indices = ids != self.num_classes  # for VOID label
            ids = ids[legal_indices]

            # # Get predicted labels
            # labels = np.array([id % INSTANCE_OFFSET for id in ids], dtype=np.int64)
            # labels = [self.obc[l] for l in labels]

            # (N_m, H, W)
            segms = pan_results[None] == ids[:, None, None]
            # Resize predicted masks
            segms = [
                mmcv.image.imresize(m.astype(float), (img_w, img_h))
                for m in segms
            ]

            # Choose colors for each instance in coco
            colormap_coco = get_colormap(len(segms))
            colormap_coco = (np.array(colormap_coco) / 255).tolist()

            viz = Visualizer(img)
            viz.overlay_instances(
                # labels=labels,
                masks=segms,
                assigned_colors=colormap_coco,
            )
            viz_img = viz.get_output().get_image()

        else:
            # Draw bboxes
            bboxes = result.refine_bboxes[:, :4]

            # Choose colors for each instance in coco
            colormap_coco = get_colormap(len(bboxes))
            colormap_coco = (np.array(colormap_coco) / 255).tolist()

            # 1-index
            labels = [self.CLASSES[l - 1] for l in result.labels]

            viz = Visualizer(img)
            viz.overlay_instances(
                labels=labels,
                boxes=bboxes,
                assigned_colors=colormap_coco,
            )
            viz_img = viz.get_output().get_image()

        viz_final = viz_img

        if out_file is not None:
            mmcv.imwrite(viz_final, out_file)

        if not (show or out_file):
            return viz_final
