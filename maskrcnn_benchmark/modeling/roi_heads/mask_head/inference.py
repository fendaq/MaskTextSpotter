# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
import numpy as np
import torch
from PIL import Image
from torch import nn
import cv2
from torch.nn import functional as F

from maskrcnn_benchmark.structures.bounding_box import BoxList

# TODO check if want to return a single BoxList or a composite
# object
class MaskPostProcessor(nn.Module):
    """
    From the results of the CNN, post process the masks
    by taking the mask corresponding to the class with max
    probability (which are of fixed size and directly output
    by the CNN) and return the masks in the mask field of the BoxList.

    If a masker object is passed, it will additionally
    project the masks in the image according to the locations in boxes,
    """

    def __init__(self, masker=None):
        super(MaskPostProcessor, self).__init__()
        self.masker = masker

    def forward(self, x, boxes):
        """
        Arguments:
            x (Tensor): the mask logits
            boxes (list[BoxList]): bounding boxes that are used as
                reference, one for ech image

        Returns:
            results (list[BoxList]): one BoxList for each image, containing
                the extra field mask
        """
        mask_prob = x.sigmoid()

        # select masks coresponding to the predicted classes
        num_masks = x.shape[0]
        labels = [bbox.get_field("labels") for bbox in boxes]
        labels = torch.cat(labels)
        index = torch.arange(num_masks, device=labels.device)
        mask_prob = mask_prob[index, labels][:, None]

        if self.masker:
            mask_prob = self.masker(mask_prob, boxes)

        boxes_per_image = [len(box) for box in boxes]
        mask_prob = mask_prob.split(boxes_per_image, dim=0)

        results = []
        for prob, box in zip(mask_prob, boxes):
            bbox = BoxList(box.bbox, box.size, mode="xyxy")
            for field in box.fields():
                bbox.add_field(field, box.get_field(field))
            bbox.add_field("mask", prob)
            results.append(bbox)

        return results
# TODO
class CharMaskPostProcessor(nn.Module):
    """
    From the results of the CNN, post process the masks
    by taking the mask corresponding to the class with max
    probability (which are of fixed size and directly output
    by the CNN) and return the masks in the mask field of the BoxList.

    If a masker object is passed, it will additionally
    project the masks in the image according to the locations in boxes,
    """

    def __init__(self, cfg, masker=None):
        super(CharMaskPostProcessor, self).__init__()
        self.masker = masker
        self.cfg = cfg

    def forward(self, x, char_mask, boxes, seq_outputs=None, seq_scores=None, detailed_seq_scores=None):
        """
        Arguments:
            x (Tensor): the mask logits
            char_mask (Tensor): the char mask logits
            boxes (list[BoxList]): bounding boxes that are used as
                reference, one for ech image

        Returns:
            results (list[BoxList]): one BoxList for each image, containing
                the extra field mask
        """
        mask_prob = x.sigmoid()
        char_mask_softmax = F.softmax(char_mask, dim=1)
        image_width, image_height = boxes[0].size
        char_results = {'char_mask': char_mask_softmax.cpu().numpy(), 'boxes': boxes[0].bbox.cpu().numpy(), 'seq_outputs': seq_outputs, 'seq_scores': seq_scores, 'detailed_seq_scores': detailed_seq_scores}
        # select masks coresponding to the predicted classes
        num_masks = x.shape[0]
        mask_prob = mask_prob.squeeze(dim=1)[:, None]

        if self.masker:
            mask_prob = self.masker(mask_prob, boxes)

        boxes_per_image = [len(box) for box in boxes]
        mask_prob = mask_prob.split(boxes_per_image, dim=0)
        results = []
        for prob, box in zip(mask_prob, boxes):
            bbox = BoxList(box.bbox, box.size, mode="xyxy")
            for field in box.fields():
                bbox.add_field(field, box.get_field(field))
            bbox.add_field("mask", prob)
            results.append(bbox)

        return [results, char_results]

class MaskPostProcessorCOCOFormat(MaskPostProcessor):
    """
    From the results of the CNN, post process the results
    so that the masks are pasted in the image, and
    additionally convert the results to COCO format.
    """

    def forward(self, x, boxes):
        import pycocotools.mask as mask_util
        import numpy as np

        results = super(MaskPostProcessorCOCOFormat, self).forward(x, boxes)
        for result in results:
            masks = result.get_field("mask").cpu()
            rles = [
                mask_util.encode(np.array(mask[0, :, :, np.newaxis], order="F"))[0]
                for mask in masks
            ]
            for rle in rles:
                rle["counts"] = rle["counts"].decode("utf-8")
            result.add_field("mask", rles)
        return results


# the next two functions should be merged inside Masker
# but are kept here for the moment while we need them
# temporarily gor paste_mask_in_image
def expand_boxes(boxes, scale):
    w_half = (boxes[:, 2] - boxes[:, 0]) * .5
    h_half = (boxes[:, 3] - boxes[:, 1]) * .5
    x_c = (boxes[:, 2] + boxes[:, 0]) * .5
    y_c = (boxes[:, 3] + boxes[:, 1]) * .5

    w_half *= scale[1]
    h_half *= scale[0]

    boxes_exp = torch.zeros_like(boxes)
    boxes_exp[:, 0] = x_c - w_half
    boxes_exp[:, 2] = x_c + w_half
    boxes_exp[:, 1] = y_c - h_half
    boxes_exp[:, 3] = y_c + h_half
    return boxes_exp


def expand_masks(mask, padding):
    N = mask.shape[0]
    M_H = mask.shape[-2]
    M_W = mask.shape[-1]
    pad2 = 2 * padding
    scale = (float(M_H + pad2) / M_H, float(M_W + pad2) / M_W)
    padded_mask = mask.new_zeros((N, 1, M_H + pad2, M_W + pad2))
    padded_mask[:, :, padding:-padding, padding:-padding] = mask
    return padded_mask, scale


def paste_mask_in_image(mask, box, im_h, im_w, thresh=0.5, padding=1):
    padded_mask, scale = expand_masks(mask[None], padding=padding)
    mask = padded_mask[0, 0]
    box = expand_boxes(box[None], scale)[0]
    box = box.numpy().astype(np.int32)

    TO_REMOVE = 1
    w = box[2] - box[0] + TO_REMOVE
    h = box[3] - box[1] + TO_REMOVE
    w = max(w, 1)
    h = max(h, 1)

    mask = Image.fromarray(mask.cpu().numpy())
    mask = mask.resize((w, h), resample=Image.BILINEAR)
    mask = np.array(mask, copy=False)

    if thresh >= 0:
        mask = np.array(mask > thresh, dtype=np.uint8)
        mask = torch.from_numpy(mask)
    else:
        # for visualization and debugging, we also
        # allow it to return an unmodified mask
        mask = torch.from_numpy(mask * 255).to(torch.uint8)

    im_mask = torch.zeros((im_h, im_w), dtype=torch.uint8)
    x_0 = max(box[0], 0)
    x_1 = min(box[2] + 1, im_w)
    y_0 = max(box[1], 0)
    y_1 = min(box[3] + 1, im_h)

    im_mask[y_0:y_1, x_0:x_1] = mask[
        (y_0 - box[1]) : (y_1 - box[1]), (x_0 - box[0]) : (x_1 - box[0])
    ]
    return im_mask


class Masker(object):
    """
    Projects a set of masks in an image on the locations
    specified by the bounding boxes
    """

    def __init__(self, threshold=0.5, padding=1):
        self.threshold = threshold
        self.padding = padding

    def forward_single_image(self, masks, boxes):
        boxes = boxes.convert("xyxy")
        im_w, im_h = boxes.size
        res = [
            paste_mask_in_image(mask[0], box, im_h, im_w, self.threshold, self.padding)
            for mask, box in zip(masks, boxes.bbox)
        ]
        if len(res) > 0:
            res = torch.stack(res, dim=0)[:, None]
        else:
            res = masks.new_empty((0, 1, masks.shape[-2], masks.shape[-1]))
        return res

    def __call__(self, masks, boxes):
        # TODO do this properly
        if isinstance(boxes, BoxList):
            boxes = [boxes]
        assert len(boxes) == 1, "Only single image batch supported"
        result = self.forward_single_image(masks, boxes[0])
        return result

def make_roi_mask_post_processor(cfg):
    masker = None
    if cfg.MODEL.CHAR_MASK_ON:
        mask_post_processor = CharMaskPostProcessor(cfg, masker)
    else:
        mask_post_processor = MaskPostProcessor(masker)
    return mask_post_processor
