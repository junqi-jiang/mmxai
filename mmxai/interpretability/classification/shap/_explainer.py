from . import _utils as utils
from ._plot import image as shap_image_plot
import shap
import numpy as np
import copy
from transformers import AutoTokenizer
from typing import List
from skimage.segmentation import slic, quickshift
import warnings


class Explainer(object):
    """ Use shap as explainer for classification models for image, text or both."""

    supported_algos = ("partition", "permutation")
    supported_modes = ("multimodal", "fix_image", "fix_text")

    def __init__(
        self,
        model,
        algorithm: str = "partition",
        max_evals: int = 300,
        batch_size: int = 50,
        tokenizer=None,
        mask_id: int = 103,
        segment_algo: str = "slic",
        n_img_segments: int = 100,
        sigma: float = 0,
        clustering: str = "braycurtis",
    ):
        """Initialise the explianer object

        Args:
            model: any model that has a .classify method as the prediction method.
                It should take a PIL image object and a string to give the classification output
            algorithm: {"partition", "permutation"}
                algorithm that is used in shap. Note that "fix_text" mode is only
                supported for "partition" algorithm
            max_evals (optional): maximum evaluation time, default 300
            batch_size (optional): batch size, default 50
            tokenizer (optional): used for text input, can be from the transformer library
                default is splitting by whitespace
            mask_id (optional): the id in tokenizer that corresponds to [MASK]

            ---- patition algorithm specific ----
            segment_algo (optional): segment algorithm {"slic", "quickshift"}
            n_img_segments (optional): used in segmentation algorithm (skimage.segmentation.slic)
                try to segment the image into approx. this many segments
            sigma (optional): width of gaussian kernel to apply smoothing before segmentation
            clustering (optional): algorithm for feature clustering, see scipy.spatial.distance.pdist
                The full list of options is:
                ‘braycurtis’(default), ‘canberra’, ‘chebyshev’, ‘cityblock’, ‘correlation’, ‘cosine’, ‘dice’,
                ‘euclidean’, ‘hamming’, ‘jaccard’, ‘jensenshannon’, ‘kulsinski’, ‘mahalanobis’,
                ‘matching’, ‘minkowski’, ‘rogerstanimoto’, ‘russellrao’, ‘seuclidean’,
                ‘sokalmichener’, ‘sokalsneath’, ‘sqeuclidean’, ‘yule’.

        """
        # input validations
        if algorithm not in self.supported_algos:
            raise ValueError(f"This algotithm {algorithm} is not supported!")

        # model should have a .classify method
        if not hasattr(model, "classify"):
            raise ValueError(f"Model object must have a .classify attribute.")

        # public features
        self.model = model
        self.algorithm = algorithm
        self.max_evals = max_evals
        self.batch_size = batch_size
        self.mask_id = mask_id  # id that corrsponds to [MASK]
        self.clustering = clustering
        self.tokenizer = utils.WhitespaceTokenizer() if tokenizer is None else tokenizer
        self.segment_algo = segment_algo
        self.n_img_segments = n_img_segments
        self.sigma = sigma

        # internal features - may be None
        # variable used in fixed mode
        self._mode = None
        self._curr_fixed = None
        # values used in multimodal mode
        self._image = None
        self._segment_map = None
        self._text_tokens = None
        self._text_ids = None

    def explain(self, images: np.ndarray, texts: np.ndarray, mode: str):
        """Main API to calculate shap values

        Args:
            images: array of shape (N, D1, D2, C);
                N = number of samples
                D1, D2, C = three channel image
            texts: array of shape (N,)
            mode: {"fix_image", "fix_text", "multimodal"}
                "fix_image": explain the multimodal input with image features fixed
                    the result will only have text features
                "fix_text": explain the multimodal input with text features fixed
                    the result will only have image features

        Returns:
            a list of shap values calculated
            a tuple of (image_shap_values, text_shap_values) is returned if mode
            is "multimodal"

        Raises:
            ValueError: if non-supported mode is entered or shape mismatch
        """
        # TODO: handle empty strings
        # input validations
        if mode not in self.supported_modes:
            raise ValueError(f"This mode {mode} is not supported!")

        if images.shape[0] != texts.shape[0]:
            raise ValueError(
                f"Shape mismatch, both inputs' first dimensions should be equal to the number of samples!"
            )

        if (mode == "fix_text") and (self.algorithm == "permutation"):
            raise ValueError(
                f"{self.algorithm} is not supported for {mode} as it will take too long!"
            )

        self._mode = mode

        if mode == "fix_image":
            # model should expect a PIL image
            images = utils.arr_to_img(images)
            # the shap algorithm will require a fully fledged tokenizer
            if isinstance(self.tokenizer, utils.WhitespaceTokenizer):
                warnings.warn(
                    "Changing tokenizer to pretrained 'distilbert-base-uncased' as fix_image mode requires a fully fledged tokenizer",
                    Warning,
                )
                self.tokenizer = AutoTokenizer.from_pretrained(
                    "distilbert-base-uncased", use_fast=True
                )
            masker = shap.maskers.Text(self.tokenizer)
            # NOTE: using shap's masker, each token in the .data attribute in shap_value contain trailing space
            # hence if needed, use "".join() to get original sentence
            return self._fixed_mode_shap_vals(texts, images, self._f_fixed_mode, masker)

        elif mode == "fix_text":
            masker = shap.maskers.Image("inpaint_telea", images[0].shape)
            return self._fixed_mode_shap_vals(images, texts, self._f_fixed_mode, masker)

        elif mode == "multimodal":
            return self._multi_mode_shap_vals(images, texts)

    @staticmethod
    def image_plot(shap_values: List, label_index: int = 0):
        """Plot 1 image chart given a list of shap values

        Args:
            shap_values: list of shap values, each of shape (D1, D2, C, #labels);
                D1, D2, C = three channel image
                #labels = number of labels in the model output

        Returns:
            a list of figures
        """
        shap_values = copy.deepcopy(shap_values)
        figs = []
        for i, value in enumerate(shap_values):
            assert (
                len(value.shape) == 4 and value.shape[2] == 3
            ), f"shap values {i} in the list should have shap (D1, D2, C, #labels)!"
            assert (
                label_index < value.base_values.shape[-1]
            ), f"label index {label_index} is not within the range of model outputs for shap value {i}!"
            shap_val = value.values[np.newaxis, ..., label_index]
            data = value.data[np.newaxis, ..., label_index].astype(float)
            # print(f"debug: {value.base_values}")
            label = f"base value: {value.base_values[label_index]:.4f}"
            figs.append(
                shap_image_plot(
                    shap_values=[shap_val],
                    pixel_values=data,
                    labels=[[label]],
                )
            )
        return figs

    @staticmethod
    def parse_text_values(shap_values: List, label_index: int = 0):
        """Plot the image chart given shap values

        Args:
            shap_values: list of shap values of shape (#tokens, #labels)
            label_index: which label the values correspond to, default 0

        Returns:
            a list of dictionarys, each containing (word: shap_value) pairs
            the shap values are relative to "base_value" which is NOT in the dictionary
        """
        out = []
        for i, value in enumerate(shap_values):
            assert (
                label_index < value.base_values.shape[-1]
            ), f"label index {label_index} is not within the range of model outputs for shap value {i}!"
            value = value[..., label_index]
            assert len(value.data) == len(value.values)
            dic = {}
            for k, v in zip(value.data, value.values):
                dic[k] = v if dic.get(k) is None else dic[k] + v
            out.append(dic)
        return out

    def _multi_mode_shap_vals(self, images, texts):
        """ Helper stud to compute shap values in multimodal mode """
        image_shap_values = []
        text_shap_values = []
        # loop through samples
        for image, text in zip(images, texts):
            # store variables for each sample
            self._image = image
            # split image into super pixels
            if self.segment_algo == "quickshift":
                self._segment_map = quickshift(image)
            else:
                self._segment_map = slic(
                    image,
                    n_segments=self.n_img_segments,
                    sigma=self.sigma,
                    start_label=0,
                )
            # get tokens for each caption
            self._text_ids = np.array(
                self.tokenizer.encode(text, add_special_tokens=False)
            )
            self._text_tokens = np.array(
                self.tokenizer.tokenize(text, add_special_tokens=False)
            )
            self._feature_split_point = np.unique(self._segment_map).shape[0]

            # initialise data (binary features)
            data_shape = (1, self._feature_split_point + self._text_ids.shape[0])
            to_explain = np.ones(data_shape, dtype=np.uint8)

            # making a masker using the "background" data which is all 0s
            masker = self._masker
            if self.algorithm == "partition":
                background = np.zeros(data_shape, dtype=np.uint8)
                masker = shap.maskers.Partition(background, clustering=self.clustering)
            elif self.algorithm == "permutation":
                # Workaround if max_evals given was too low, increase to acceptable value
                self.max_evals = self._min_acceptable_evals(to_explain.shape[1])
            else:
                pass

            # compute shap values for one pair of sample
            explainer = shap.Explainer(
                self._f_multimodal, masker, algorithm=self.algorithm
            )
            mm_shap_values = explainer(
                to_explain, max_evals=self.max_evals, batch_size=self.batch_size
            )[0]
            # select the only first element since output shape will have (1, ...)

            # append to output lists
            image_shap_values.append(
                self._build_img_shap_values(
                    mm_shap_values[: self._feature_split_point, ...],
                    self._segment_map,
                    image=image,
                )
            )
            text_shap_values.append(
                self._build_txt_shap_values(
                    mm_shap_values[self._feature_split_point :, ...],
                    self._text_tokens,
                )
            )

        return image_shap_values, text_shap_values

    def _fixed_mode_shap_vals(self, X, to_fix, func, masker):
        """ Helper stud to compute shap values in single-modal mode where one sample is fixed """
        assert len(X) == len(to_fix)
        out = []
        explainer = shap.Explainer(func, masker, algorithm=self.algorithm)
        last_shape = X[0].shape
        # loop through samples
        for i in range(len(X)):
            self._curr_fixed = to_fix[i]
            if self._mode == "fix_text" and X[i].shape != last_shape:
                # reinitialise explainer using new masker if shape different
                masker = shap.maskers.Image("inpaint_telea", X[i].shape)
                explainer = shap.Explainer(func, masker, algorithm=self.algorithm)
                last_shape = X[i].shape
            if self._mode == "fix_image" and self.algorithm == "permutation":
                # Workaround if max_evals given was too low, increase to acceptable value
                self.max_evals = self._min_acceptable_evals(
                    len(self.tokenizer.tokenize(X[i]))
                )
            values = explainer(
                X[i : i + 1], max_evals=self.max_evals, batch_size=self.batch_size
            )
            out.append(values[0])  # select the only first element
        return out

    def _f_multimodal(self, combined_features: np.ndarray):
        """Function/Class description

        Args:
            combined_features: binary segment mask matrix of shape (#samples, #features)
                # features = segments + word tokens

        Returns:
            what is returned:

        """
        out = np.zeros((len(combined_features), 2))  # output same shape

        image_features = combined_features[:, : self._feature_split_point]
        text_features = combined_features[:, self._feature_split_point :]
        images = [self._image_from_features(m) for m in image_features]
        texts = [self._text_from_features(m) for m in text_features]

        # input neeeds to be [PIL.Image.Image], so transform from array
        images = utils.arr_to_img(images)

        for i, (text, image) in enumerate(zip(texts, images)):
            # classify, output is a tupe (index, score)
            ind, score = self.model.classify(image, text).values()
            out[i][ind] = score
            out[i][1 - ind] = 1 - score

        return out

    def _f_fixed_mode(self, X: np.ndarray):
        """Prediction function used in fixed mode

        Args:
            X: could be array of images or texts
                - images: np.ndarray of shape (N, D1, D2, C);
                    N = number of samples
                    D1, D2, C = three channel image
                - texts: np.ndarray[str] of shape (N,)
                    N = number of samples

        Returns:
            numpy array of predictions with shape = (N, 2);
                - N[i] = score of the image being i

        """
        out = np.zeros((len(X), 2))  # output same shape

        # inputs neeeds to be [PIL.Image.Image], so transform from array if explaining image
        X = utils.arr_to_img(X) if self._mode == "fix_text" else X
        # select fixed side, could be a strings or PIL image
        fixed = self._curr_fixed

        for i, x in enumerate(X):
            # classify should take (image, text), output is a tuple (index, score)
            # if mode is "fix_text" fixed should be second and input will be images
            ind, score = (
                self.model.classify(x, fixed).values()
                if self._mode == "fix_text"
                else self.model.classify(fixed, x).values()
            )

            out[i][ind] = score
            out[i][1 - ind] = 1 - score

        return out

    # ===== helper functions =====
    def _min_acceptable_evals(self, num_features):
        if self.max_evals <= (2 * num_features + 1):
            new_evals = 2 * num_features + 2
            warnings.warn(
                f"Setting max evals to be {new_evals} as required by SHAP, otherwise an error is thrown.",
                Warning,
            )
            return new_evals
        else:
            return self.max_evals

    def _text_from_features(self, features, skip_special_tokens=False):
        """TODO: docs

        Args:
            mask: binary mask representing features

        Returns:
            what is returned:

        """
        out = np.array(self._text_ids)
        out[features == 0] = self.mask_id
        return self.tokenizer.decode(out, skip_special_tokens=skip_special_tokens)

    def _image_from_features(self, features):
        """TODO: docs

        Args:
            features: binary features indicating if the segment is active or not

        Returns:
            what is returned:

        Raises:
            KeyError: An example
        """
        segments_to_keep = np.unique(self._segment_map)[features == 1]
        # setup mask with shape as original image
        mask = np.in1d(self._segment_map, segments_to_keep).reshape(
            self._segment_map.shape
        )
        mask = np.repeat(mask[..., np.newaxis], 3, axis=2)  # turn the mask to 3d
        masked_img = mask * self._image
        return masked_img

    @staticmethod
    def _masker(mask, x):
        """
        TODO: docs
        simple masker (turn values 0) used in permutation method
        """
        return (x * mask).reshape(1, len(x))

    @staticmethod
    def _build_img_shap_values(
        shap_values: shap.Explanation, segment_map: np.ndarray, image: np.ndarray
    ):
        """TODO: docs

        Args:
            shap_values: shap values for 1 sample, shape = (#segments, #labels)
            segment_map: an 2-d array, each element is the segment number for the pixel in
                the original image
            image: original image to explain, shape (H, W, C)

        Returns:
            shap.Explanation object, shape (1, 3D_image_shape, #labels)
            image shap values where data is the original image and the shap value
            of each pixel is the average across the entire segment

        """
        assert (
            len(shap_values.shape) == 2
        ), "shap_values should have shape (#segments, #labels)"
        assert len(image.shape) == 3, "image should have shape (1, H, W, C)"
        # build output array
        out = np.zeros(image.shape + (shap_values.shape[-1],))
        num_channels = 3
        # loop through each segment's shap values - vals.shape = #labels
        for seg, vals in enumerate(shap_values):
            # calculate the average for each pixel in each channel
            condition = segment_map == seg  # filter by segment
            shaps_per_pixel = vals.values / (np.sum(condition) * num_channels)
            out[condition] = shaps_per_pixel
        # explanation object
        shap_val = shap.Explanation(
            values=out, base_values=shap_values.base_values, data=image.astype(float)
        )
        shap_val.segment_map = segment_map  # store segment map for current image
        return shap_val

    @staticmethod
    def _build_txt_shap_values(shap_values, original_tokens):
        assert (
            shap_values.shape[0] == original_tokens.shape[0]
        ), "need same number of shap values and tokens"
        return shap.Explanation(
            values=shap_values.values,
            base_values=shap_values.base_values,
            data=original_tokens,
        )
