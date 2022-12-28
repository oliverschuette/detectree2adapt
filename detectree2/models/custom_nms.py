from typing import Tuple

import torch
import torchvision
from torch import Tensor
from torchvision.extension import _assert_has_ops
import logging
import math
from typing import List, Tuple, Union

from detectron2.layers import batched_nms, cat
from detectron2.structures import Boxes, Instances

from detectron2.structures import Boxes, Instances
from typing import Dict, List, Optional, Tuple, Union
import torch
import torch.nn.functional as F
from torch import nn

from detectron2.config import configurable
from detectron2.layers import Conv2d, ShapeSpec, cat
from detectron2.structures import Boxes, ImageList, Instances, pairwise_iou
from detectron2.utils.events import get_event_storage
from detectron2.utils.memory import retry_if_cuda_oom
from detectron2.utils.registry import Registry
from detectron2.modeling.proposal_generator.rpn import RPN, build_rpn_head

from detectron2.modeling.anchor_generator import build_anchor_generator
from detectron2.modeling.box_regression import Box2BoxTransform, _dense_box_regression_loss
from detectron2.modeling.matcher import Matcher
from detectron2.modeling.sampling import subsample_labels
from detectron2.modeling.proposal_generator.build import PROPOSAL_GENERATOR_REGISTRY
from detectron2.modeling import build_model


@torch.jit.script_if_tracing
def move_device_like(src: torch.Tensor, dst: torch.Tensor) -> torch.Tensor:
    """
    Tracing friendly way to cast tensor to another tensor's device. Device will be treated
    as constant during tracing, scripting the casting process as whole can workaround this issue.
    """
    return src.to(dst.device)
  
def _is_tracing():
    # (fixed in TORCH_VERSION >= 1.9)
    if torch.jit.is_scripting():
        # https://github.com/pytorch/pytorch/issues/47379
        return False
    else:
        return torch.jit.is_tracing()
      
@PROPOSAL_GENERATOR_REGISTRY.register()
class custom_RPN(RPN):

  @configurable
  def __init__(self,
        *,
        in_features: List[str],
        head: nn.Module,
        anchor_generator: nn.Module,
        anchor_matcher: Matcher,
        box2box_transform: Box2BoxTransform,
        batch_size_per_image: int,
        positive_fraction: float,
        pre_nms_topk: Tuple[float, float],
        post_nms_topk: Tuple[float, float],
        nms_thresh: float = 0.4,
        nms_thresh_union: float = 0.4,
        min_box_size: float = 0.0,
        anchor_boundary_thresh: float = -1.0,
        loss_weight: Union[float, Dict[str, float]] = 1.0,
        box_reg_loss_type: str = "smooth_l1",
        smooth_l1_beta: float = 0.0,):
    super().__init__(in_features=in_features, head=head, anchor_generator=anchor_generator, 
                 anchor_matcher=anchor_matcher, box2box_transform=box2box_transform, batch_size_per_image=batch_size_per_image, 
                 positive_fraction=positive_fraction, pre_nms_topk=pre_nms_topk, post_nms_topk=post_nms_topk)
    self.nms_thresh_union = nms_thresh_union

  @classmethod
  def from_config(cls, cfg, input_shape: Dict[str, ShapeSpec]):
    in_features = cfg.MODEL.RPN.IN_FEATURES
    ret = {
        "in_features": in_features,
        "min_box_size": cfg.MODEL.PROPOSAL_GENERATOR.MIN_SIZE,
        "nms_thresh": cfg.MODEL.RPN.NMS_THRESH,
        "nms_thresh_union": cfg.nms_thresh_union,
        "batch_size_per_image": cfg.MODEL.RPN.BATCH_SIZE_PER_IMAGE,
        "positive_fraction": cfg.MODEL.RPN.POSITIVE_FRACTION,
        "loss_weight": {
            "loss_rpn_cls": cfg.MODEL.RPN.LOSS_WEIGHT,
            "loss_rpn_loc": cfg.MODEL.RPN.BBOX_REG_LOSS_WEIGHT * cfg.MODEL.RPN.LOSS_WEIGHT,
        },
        "anchor_boundary_thresh": cfg.MODEL.RPN.BOUNDARY_THRESH,
        "box2box_transform": Box2BoxTransform(weights=cfg.MODEL.RPN.BBOX_REG_WEIGHTS),
        "box_reg_loss_type": cfg.MODEL.RPN.BBOX_REG_LOSS_TYPE,
        "smooth_l1_beta": cfg.MODEL.RPN.SMOOTH_L1_BETA,
    }

    ret["pre_nms_topk"] = (cfg.MODEL.RPN.PRE_NMS_TOPK_TRAIN, cfg.MODEL.RPN.PRE_NMS_TOPK_TEST)
    ret["post_nms_topk"] = (cfg.MODEL.RPN.POST_NMS_TOPK_TRAIN, cfg.MODEL.RPN.POST_NMS_TOPK_TEST)

    ret["anchor_generator"] = build_anchor_generator(cfg, [input_shape[f] for f in in_features])
    ret["anchor_matcher"] = Matcher(
        cfg.MODEL.RPN.IOU_THRESHOLDS, cfg.MODEL.RPN.IOU_LABELS, allow_low_quality_matches=True
    )
    ret["head"] = build_rpn_head(cfg, [input_shape[f] for f in in_features]) 
    return ret

  def find_top_rpn_proposals(
    proposals: List[torch.Tensor],
    pred_objectness_logits: List[torch.Tensor],
    image_sizes: List[Tuple[int, int]],
    nms_thresh: float,
    nms_thresh_union: float,
    pre_nms_topk: int,
    post_nms_topk: int,
    min_box_size: float,
    training: bool,
):
    # here the box refinement has been done when proposals are inputted 
    num_images = len(image_sizes)
    device = (
        proposals[0].device
        if torch.jit.is_scripting()
        else ("cpu" if torch.jit.is_tracing() else proposals[0].device)
    )

    # 1. Select top-k anchor for every level and every image
    topk_scores = []  # #lvl Tensor, each of shape N x topk
    topk_proposals = []
    batch_idx = move_device_like(torch.arange(num_images, device=device), proposals[0])
    for level_id, (proposals_i, logits_i) in enumerate(zip(proposals, pred_objectness_logits)):
        Hi_Wi_A = logits_i.shape[1]
        if isinstance(Hi_Wi_A, torch.Tensor):  # it's a tensor in tracing
            num_proposals_i = torch.clamp(Hi_Wi_A, max=pre_nms_topk)
        else:
            num_proposals_i = min(Hi_Wi_A, pre_nms_topk)

        topk_scores_i, topk_idx = logits_i.topk(num_proposals_i, dim=1)

        # each is N x topk
        topk_proposals_i = proposals_i[batch_idx[:, None], topk_idx]  # N x topk x 4

        topk_proposals.append(topk_proposals_i)
        topk_scores.append(topk_scores_i)


    # 2. Concat all levels together
    topk_scores = cat(topk_scores, dim=1)
    topk_proposals = cat(topk_proposals, dim=1)

    # 3. For each image, run a per-level NMS, and choose topk results.
    results: List[Instances] = []
    for n, image_size in enumerate(image_sizes):
        print("----------------------------------------", image_size)
        boxes = Boxes(topk_proposals[n])
        scores_per_img = topk_scores[n]

        valid_mask = torch.isfinite(boxes.tensor).all(dim=1) & torch.isfinite(scores_per_img)
        if not valid_mask.all():
            if training:
                raise FloatingPointError(
                    "Predicted boxes or scores contain Inf/NaN. Training has diverged."
                )
            boxes = boxes[valid_mask]
            scores_per_img = scores_per_img[valid_mask]
        boxes.clip(image_size)

        # filter empty boxes
        keep = boxes.nonempty(threshold=min_box_size)
        if _is_tracing() or keep.sum().item() != len(boxes):
            boxes, scores_per_img= boxes[keep], scores_per_img[keep]

        keep = custom_nms(boxes.tensor, scores_per_img, nms_thresh_union)

        keep = keep[:post_nms_topk]  # keep is already sorted

        res = Instances(image_size)
        res.proposal_boxes = boxes[keep]
        res.objectness_logits = scores_per_img[keep]
        results.append(res)
    return results
  
def custom_nms(P : torch.tensor ,scores: torch.tensor, thresh_iou_o : float):
    """
    Apply non-maximum suppression to avoid detecting too many
    overlapping bounding boxes for a given object.
    Args:
        boxes: (tensor) The location preds for the image 
            along with the class predscores, Shape: [num_boxes,5].
        thresh_iou: (float) The overlap thresh for suppressing unnecessary boxes.
    Returns:
        A list of filtered boxes index, Shape: [ , 1]
    """
 
    # we extract coordinates for every 
    # prediction box present in P
    x1 = P[:, 0]
    y1 = P[:, 1]
    x2 = P[:, 2]
    y2 = P[:, 3]
 
    # we extract the confidence scores as well
    scores = scores
 
    # calculate area of every block in P
    areas = (x2 - x1) * (y2 - y1)
     
    # sort the prediction boxes in P
    # according to their confidence scores
    order = scores.argsort()
 
    # initialise an empty list for 
    # filtered prediction boxes
    keep = []
     
 
    while len(order) > 0:
         
        # extract the index of the 
        # prediction with highest score
        # we call this prediction S
        idx = order[-1]
 
        # push S in filtered predictions list
        keep.append(idx)
 
        # remove S from P
        order = order[:-1]
 
        # sanity check
        if len(order) == 0:
            break
         
        # select coordinates of BBoxes according to 
        # the indices in order
        xx1 = torch.index_select(x1,dim = 0, index = order)
        xx2 = torch.index_select(x2,dim = 0, index = order)
        yy1 = torch.index_select(y1,dim = 0, index = order)
        yy2 = torch.index_select(y2,dim = 0, index = order)
 
        # find the coordinates of the intersection boxes
        xx1 = torch.max(xx1, x1[idx])
        yy1 = torch.max(yy1, y1[idx])
        xx2 = torch.min(xx2, x2[idx])
        yy2 = torch.min(yy2, y2[idx])
 
        # find height and width of the intersection boxes
        w = xx2 - xx1
        h = yy2 - yy1
         
        # take max with 0.0 to avoid negative w and h
        # due to non-overlapping boxes
        w = torch.clamp(w, min=0.0)
        h = torch.clamp(h, min=0.0)
 
        # find the intersection area
        inter = w*h
 
        # find the areas of BBoxes according the indices in order
        rem_areas = torch.index_select(areas, dim = 0, index = order) 

        # find the interaction over S
        IoU_S = inter / areas[idx]

        # find the interaction over prediction
        IoU_P = inter / areas
 
        # keep the boxes with IoU less than thresh_iou
        mask = (IoU_S < thresh_iou_o)&(IoU_P < thresh_iou_o)
        order = order[mask]
     
    return keep
  
def custom_nms_mask(P : torch.tensor ,scores: torch.tensor, thresh_iou_o : float):
    """
    Apply non-maximum suppression to avoid detecting too many
    overlapping bounding boxes for a given object.
    Args:
        masks: (tensor) The location preds for the image 
            along with the class predscores, Shape: [n,image_shape,image_shape].
        thresh_iou: (float) The overlap thresh for suppressing unnecessary boxes.
    Returns:
        A list of filtered boxes index, Shape: [ , 1]
    """
 
    # we turn masks into ndarray
    masks = P.reshape(len(P),-1,1)
 
    # we extract the confidence scores as well
    scores = scores
 
    # calculate area of every block in P
    areas = torch.sum(masks, axis = 1)
     
    # sort the prediction boxes in P
    # according to their confidence scores
    order = scores.argsort()
 
    # initialise an empty list for 
    # filtered prediction boxes
    keep = []
     
 
    while len(order) > 0:
         
        # extract the index of the 
        # prediction with highest score
        # we call this prediction S
        idx = order[-1]
 
        # push S in filtered predictions list
        keep.append(idx)
 
        # remove S from P
        order = order[:-1]
 
        # sanity check
        if len(order) == 0:
            break
 
        # find the areas of BBoxes according the indices in order
        rem_areas = areas[order]

        # find the intersection area
        inter = torch.sum(masks[idx] * masks[order], axis=1)

        # find the interaction over S
        IoU_S = inter / areas[idx]

        # find the interaction over prediction
        IoU_P = inter / rem_areas
 
        # keep the masks with IoU less than thresh_iou
        mask = (IoU_S < thresh_iou_o)&(IoU_P < thresh_iou_o).reshape(-1,1)
        order = order.reshape(-1,1)[mask]
     
    return keep

class DefaultPredictor1:
    """
    intersection over area and nms is added here
    """
    def __init__(self, cfg):
        self.cfg = cfg.clone()  # cfg can be modified by model
        self.model = build_model(self.cfg)
        self.model.eval()
        if len(cfg.DATASETS.TEST):
            self.metadata = MetadataCatalog.get(cfg.DATASETS.TEST[0])

        checkpointer = DetectionCheckpointer(self.model)
        checkpointer.load(cfg.MODEL.WEIGHTS)

        self.aug = T.ResizeShortestEdge(
            [cfg.INPUT.MIN_SIZE_TEST, cfg.INPUT.MIN_SIZE_TEST], cfg.INPUT.MAX_SIZE_TEST
        )

        self.input_format = cfg.INPUT.FORMAT
        assert self.input_format in ["RGB", "BGR"], self.input_format

    def __call__(self, original_image):
        """
        Args:
            original_image (np.ndarray): an image of shape (H, W, C) (in BGR order).
        Returns:
            predictions (dict):
                the output of the model for one image only.
                See :doc:`/tutorials/models` for details about the format.
        """
        with torch.no_grad():  # https://github.com/sphinx-doc/sphinx/issues/4258
            # Apply pre-processing to image.
            if self.input_format == "RGB":
                # whether the model expects BGR inputs or RGB
                original_image = original_image[:, :, ::-1]
            height, width = original_image.shape[:2]
            image = self.aug.get_transform(original_image).apply_image(original_image)
            image = torch.as_tensor(image.astype("float32").transpose(2, 0, 1))

            inputs = {"image": image, "height": height, "width": width}
            predictions = self.model([inputs])[0]

            boxes = predictions['instances'].get_fields()['pred_boxes']
            scores = predictions['instances'].get_fields()['scores']
            pred_classes = predictions['instances'].get_fields()['pred_classes']
            pred_masks = predictions['instances'].get_fields()['pred_masks']

            keep = custom_nms_mask(pred_masks ,scores, thresh_iou_o = 0.3)

            predictions['instances'].get_fields()['pred_boxes'] = boxes[keep]
            predictions['instances'].get_fields()['scores'] = scores[keep]
            predictions['instances'].get_fields()['pred_classes'] = pred_classes[keep]
            predictions['instances'].get_fields()['pred_masks'] = pred_masks[keep]

            return predictions
