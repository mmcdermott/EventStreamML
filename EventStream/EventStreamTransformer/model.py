import math, torch
from typing import Any, Callable, Dict, Optional, Set, Tuple, Union

from ..EventStreamData.types import DataModality, EventStreamPytorchBatch
from ..EventStreamData.data_embedding_layer import MeasIndexGroupOptions
from .config import (
    StructuredEventProcessingMode,
    StructuredEventStreamTransformerConfig,
    TimeToEventGenerationHeadType,
)
from .generation_utils import StructuredEventStreamGenerationMixin
from .generative_layers import (
    ExponentialTTELayer,
    LogNormalMixtureTTELayer,
    GaussianIndexedRegressionLayer,
)
from .model_output import (
    GenerativeSequenceModelLosses,
    GenerativeSequenceModelPredictions,
    GenerativeSequenceModelLabels,
    EventStreamTransformerForGenerativeSequenceModelOutput,
    EventStreamTransformerForStreamClassificationModelOutput,
)
from .transformer import (
    StructuredEventStreamTransformerPreTrainedModel, StructuredEventStreamTransformer
)
from .utils import safe_masked_max, safe_weighted_avg, weighted_loss

class StructuredEventStreamGenerativeOutputLayer(torch.nn.Module):
    # TODO(mmd): Allow for use of NLL-beta throughout?
    # TODO(mmd): Per-subject, NLL should be averaged over total duration, not # of events?
    def __init__(
            self,
            config: StructuredEventStreamTransformerConfig,
        ):
        super().__init__()

        self.config = config

        match self.config.TTE_generation_layer_type:
            case TimeToEventGenerationHeadType.LOG_NORMAL_MIXTURE:
                self.TTE_layer = LogNormalMixtureTTELayer(
                    in_dim         = config.hidden_size,
                    num_components = config.TTE_lognormal_generation_num_components,
                    mean_log_inter_time = config.mean_log_inter_event_time_min,
                    std_log_inter_time = config.std_log_inter_event_time_min,
                )
            case TimeToEventGenerationHeadType.EXPONENTIAL:
                self.TTE_layer = ExponentialTTELayer(in_dim = config.hidden_size)
            case _:
                raise ValueError(
                    f"Invalid option for `config.TTE_generation_layer_type`. Must be "
                    f"a member of the `TimeToEventGenerationHeadType` enum: "
                    f"({TimeToEventGenerationHeadType.values()}). got {config.TTE_generation_layer_type}."
                )

        self.ClassificationLayer = torch.nn.Linear(config.hidden_size, config.vocab_size)

        self.classification_criteria = {}
        for measurement in config.measurements_per_generative_mode[DataModality.SINGLE_LABEL_CLASSIFICATION]:
            self.classification_criteria[measurement] = torch.nn.CrossEntropyLoss(reduction='none')
        for measurement in config.measurements_per_generative_mode[DataModality.MULTI_LABEL_CLASSIFICATION]:
            self.classification_criteria[measurement] = torch.nn.BCEWithLogitsLoss(reduction='none')

        self.regression_layers   = torch.nn.ModuleDict({})
        for measurement in config.measurements_per_generative_mode[DataModality.MULTIVARIATE_REGRESSION]:
            self.regression_layers[measurement] = GaussianIndexedRegressionLayer(
                n_regression_targets = config.vocab_sizes_by_measurement[measurement],
                in_dim = config.hidden_size,
            )

        self.classification_mode_per_measurement = {}
        for generative_mode, measurements in config.measurements_per_generative_mode.items():
            if generative_mode == DataModality.MULTIVARIATE_REGRESSION: continue
            for measurement in measurements:
                assert measurement not in self.classification_mode_per_measurement
                self.classification_mode_per_measurement[measurement] = generative_mode

    def get_TTE_outputs(self, batch: EventStreamPytorchBatch, encoded: torch.FloatTensor) -> Tuple[
        torch.FloatTensor, torch.distributions.Distribution, torch.FloatTensor,
    ]:
        """
        Produces time-to-event predictions and log likelihoods (_not NLLs!_) for the model.

        Args:
            `batch` (`EventStreamPytorchBatch`):
                The batch of data for which the classification predictions are desired.
            `encoded` (`torch.FloatTensor`, shape is batch_size X sequence_length X hidden_dim):
                The final encodings _to be used to predict the time from the event at that position to the
                subsequent event_. For example, the vector `encoded[i][j]` (which is of size `hidden_dim` and
                corresponds to event `j` for batch element `i`) is
                _not_ used to predict the time from event `j-1` to event `j`, but rather is used to predict the
                time from event `j` to event `j+1` (all for batch index `i`, of course). _Note that this is
                shifted from how `encoded` is used in other functions in this class._

        Returns:
            `TTE_LL` (`torch.FloatTensor`):
                A torch scalar containing the average log-likelihood of observed time-to-events given the
                predicted distribution. Averaging is done over all unmasked events per batch element first,
                then in a macro manner over all batch elements.
                TODO(mmd): Should probably be NLL, not LL.
            `TTE_dist` (`torch.distributions.Distribution`):
                The predicted torch Distribution for modelling time-to-event. The distribution's shape is such
                that samples drawn from the distribution will have shape `[batch_size, sequence_length]` and
                `sample[i][j]` will be a prediction for the time between events `j` and `j+1` for batch
                element `i` (note that this includes a prediction for the time until the event after the end
                of the sequence, though such an event is naturally not observed).
            `TTE_true` (`torch.FloatTensor`):
                A tensor of shape `[batch_size, sequence_length - 1]` such that `TTE_true[i][j]` contains the
                observed time between events `j` and `j+1` for batch element `i`.
        """
        TTE_dist = self.TTE_layer(encoded)

        # TTE_dist is a distribution with random variables of shape (batch size, sequence length)
        TTE_obs_mask = (batch['event_mask'][:, 1:] & batch['event_mask'][:, :-1])
        TTE_delta    = batch['time'].diff()
        TTE_true     = torch.where(TTE_obs_mask, TTE_delta, torch.ones_like(TTE_delta))

        # As TTE_dist contains a predicted distribution for the last sequence element, which we want to return
        # for generative purposes, we add a fake observation to the last element.
        TTE_true_exp = torch.cat((TTE_true, torch.ones_like(TTE_true[:, -1]).unsqueeze(-1)), dim=-1)
        TTE_obs_mask_exp = torch.cat(
            (TTE_obs_mask, torch.zeros_like(TTE_obs_mask[:, -1]).unsqueeze(-1)), dim=-1
        )

        # We skip the last event as we have no true time to event for that event.
        # TODO(mmd): Use NLL-\beta?
        TTE_LL = TTE_dist.log_prob(TTE_true_exp)

        if TTE_obs_mask_exp.isnan().any():
            raise ValueError(f"NaNs in TTE_obs_mask_exp: {batch}")
        elif TTE_true_exp.isnan().any():
            raise ValueError(f"NaNs in TTE_true_exp: {batch}")
        elif TTE_LL.isnan().any():
            raise ValueError(f"NaNs in TTE_LL: {batch}")
        elif (TTE_obs_mask_exp.float().sum(-1) == 0).any():
            raise ValueError(f"No observed time-to-event for >= 1 patient in batch: {batch}")

        TTE_LL_per_patient = (TTE_LL * TTE_obs_mask_exp.float()).sum(-1) / TTE_obs_mask_exp.float().sum(-1)
        TTE_LL_overall     = TTE_LL_per_patient.mean()

        return TTE_LL_overall, TTE_dist, TTE_true

    def get_classification_outputs(
        self, batch: EventStreamPytorchBatch, encoded: torch.FloatTensor, valid_measurements: Set[str],
        event_type_mask_per_measurement: Optional[Dict[str, torch.BoolTensor]] = None,
    ) -> Tuple[
        Dict[str, torch.FloatTensor],
        Dict[str, torch.FloatTensor],
        Dict[str, Union[torch.LongTensor, torch.FloatTensor]],
    ]:
        """
        Produces classification predictions and losses for the model.

        Args:
            `batch` (`EventStreamPytorchBatch`):
                The batch of data for which the classification predictions are desired.
            `encoded` (`torch.FloatTensor`, shape is batch_size X sequence_length X hidden_dim):
                The final encodings _to be used to predict for each position in the sequence_. For example,
                the vector `encoded[i][j]` (which is of size `hidden_dim`) is _not_ the summary encoding of
                the batch element at batch index `i` and sequence index `j`, but rather is the input to be
                used to form classification predictions corresponding to batch element `i` at sequence
                position `j`.
            `valid_measurements` (`Set[str]`):
                The classification measurements in the batch that should be predicted from this input `encoded`.
            `event_type_mask_per_measurement` (`Optional[Dict[str, torch.BoolTensor]]`, defaults to None):
                A dictionary from measurement to a tensor of shape `[batch_size, sequence_length]` such that
                `event_type_mask_per_measurement[measurement][i][j]` is `True` if the event at batch index `i`
                and sequence index `j` is of a type that should be used to form predictions for the
                measurement `measurement`. If `None`, then all events are used to form predictions for all
                measurements.

        Returns:
            `classification_losses_by_measurement` (`Dict[str, torch.FloatTensor]`):
                A dictionary from `measurement` to scalar tensors consisting of the average NLL of the data
                given the classiciation model. Averaging happens via the following procedure:
                  * For multi-label measurements:
                    1. NLL is averaged over labels per sequence event, for all unmasked sequence events (as in
                       theory any event could have observed labels for binary multi-lable predictions).
                       TODO(mmd): this should likely be specific to events with certain event types.
                    2. NLL is macro-averaged across unmasked sequence events per batch element.
                    3. NLL is macro-averaged across batch elements.
                  * For single-task measurements:
                    1. NLL is computed on any event that has a label for that task.
                       TODO(mmd): Maybe should be conditioned on specific event types too?
                    2. NLL is macro-averaged across events which had a label for that task per sequence.
                       Sequences without any events with that label receive a loss of zero.
                    3. NLL is macro-averaged across batch elements.
            `classification_dists_by_measurement` (`Dict[str, torch.FloatTensor]`):
                A dictionary from `measurement` to classification distributions of shape
                `[batch_size X sequence_length X vocabulary_size]` or `[batch_size X sequence_length]`
                reflecting the probabilities for each event for that measurement. Returns scores for all events,
                even those that are masked, including the final event.
            `classification_labels_by_measurement` (`Dict[str, Union[torch.LongTensor, torch.FloatTensor]]`):
                A dictionary from `measurement` to tensors of one of two types:
                  * For multi-label measurements, returns FloatTensors of shape
                    `[batch_size X sequence_length X vocabulary_size]` containing binary labels for each
                    vocabulary element for each event.
                  * For single-label measurements, returns LongTensors of shape
                    `[batch_size, sequence_length]` containing label indices for each event with that task
                    observed, otherwise contains zeros.
        """

        if not valid_measurements: return {}, {}, {}

        # Classification of what elements are going to occur:
        classification_scores = self.ClassificationLayer(encoded)

        classification_losses_by_measurement = {}
        classification_dists_by_measurement = {}
        classification_labels_by_measurement = {}

        for measurement, classification_mode in self.classification_mode_per_measurement.items():
            if measurement not in valid_measurements: continue

            if event_type_mask_per_measurement is not None:
                event_mask = event_type_mask_per_measurement[measurement] & batch['event_mask']
            else:
                event_mask = batch['event_mask']

            measurement_idx = self.config.measurements_idxmap[measurement]
            vocab_start   = self.config.vocab_offsets_by_measurement[measurement]
            vocab_end     = min(
                o for o in list(self.config.vocab_offsets_by_measurement.values()) + [self.config.vocab_size]\
                if o > vocab_start
            )

            scores = classification_scores[:, :, vocab_start:vocab_end]
            # scores is of shape [batch X seq X vocab_end-vocab_start]

            # We don't need to shift here, as given this is a structured model, we'll always rely on elements
            # of the dependency graph that don't include these inputs to predict them (e.g., predict the
            # contents of the event given the time at which the event occurred).
            dynamic_indices = batch['dynamic_indices']
            tensor_idx = (batch['dynamic_measurement_indices'] == measurement_idx)

            if classification_mode == DataModality.SINGLE_LABEL_CLASSIFICATION:
                # As there is only one index of this type for this setting,
                # we can direclty multiply by the mask and sum
                events_with_label = tensor_idx.any(dim=-1)
                labels  = (
                    (dynamic_indices.long() * tensor_idx.long()).sum(dim=-1) -
                    vocab_start
                ) * events_with_label.long()
                # labels is of shape [batch X seq]

                loss_per_event = self.classification_criteria[measurement](scores.transpose(1, 2), labels)

                event_mask = event_mask & events_with_label

                dists = torch.distributions.Categorical(logits=scores)

            elif classification_mode == DataModality.MULTI_LABEL_CLASSIFICATION:
                data_labels_or_zero = torch.where(
                    tensor_idx,
                    dynamic_indices - vocab_start + 1, # Add an extra 1 so zero is always omitted.
                    torch.zeros_like(dynamic_indices),
                ).long()

                labels = torch.zeros(
                    scores.shape[0], scores.shape[1], 1+scores.shape[2], device=scores.device
                ).scatter(
                    dim=2, index=data_labels_or_zero, value=1,
                )

                labels = labels[:, :, 1:] # Drop the omitted labels...

                loss_per_label = self.classification_criteria[measurement](scores, labels)
                loss_per_event = loss_per_label.mean(dim=-1)

                dists = torch.distributions.Bernoulli(logits=scores)

            else: raise ValueError

            loss_overall = weighted_loss(loss_per_event, event_mask)

            classification_losses_by_measurement[measurement] = loss_overall
            classification_dists_by_measurement[measurement] = dists
            classification_labels_by_measurement[measurement] = labels
        return (
            classification_losses_by_measurement, classification_dists_by_measurement,
            classification_labels_by_measurement
        )

    def get_regression_outputs(
        self, batch: EventStreamPytorchBatch, encoded: torch.FloatTensor, valid_measurements: Set[str],
        is_generation: bool = False,
        event_type_mask_per_measurement: Optional[Dict[str, torch.BoolTensor]] = None,
    ) -> Tuple[
        Dict[str, torch.FloatTensor],
        Dict[str, torch.distributions.Distribution],
        Dict[str, torch.FloatTensor],
        Dict[str, torch.LongTensor],
    ]:
        """
        Produces regression predictions and losses for the model.

        Args:
            `batch` (`EventStreamPytorchBatch`):
                The batch of data for which the regression predictions are desired.
            `encoded` (`torch.FloatTensor`, shape is batch_size X sequence_length X hidden_dim):
                The final encodings _to be used to predict for each position in the sequence_. For example,
                the vector `encoded[i][j]` (which is of size `hidden_dim`) is _not_ the summary encoding of
                the batch element at batch index `i` and sequence index `j`, but rather is the input to be
                used to form regression predictions corresponding to batch element `i` at sequence
                position `j`.
            `valid_measurements` (`Set[str]`):
                The regression measurements in the batch that should be predicted from this input `encoded`.
            `event_type_mask_per_measurement` (`Optional[Dict[str, torch.BoolTensor]]`, defaults to None):
                A dictionary from measurement to a tensor of shape `[batch_size, sequence_length]` such that
                `event_type_mask_per_measurement[measurement][i][j]` is `True` if the event at batch index `i`
                and sequence index `j` is of a type that should be used to form predictions for the
                measurement `measurement`. If `None`, then all events are used to form predictions for all
                measurements.

        Returns:
            `regression_loss_values` (`Dict[str, torch.FloatTensor]`):
                A dictionary from `measurement` to scalar tensors consisting of the average NLL of the data
                given the regression model. Averaging happens via the following procedure:
                  1. NLL is averaged over data elements of the correct measurement per event.
                     TODO(mmd): This is likely a bit wrong; if a regression task has no observed value, that
                     should be taken into account here but I don't think it is currently.
                  2. Per-event NLLs are averaged over unmasked events with labels per batch element.
                  3. NLL is macro-averaged over the batch.
            `regression_dists` (`Dict[str, torch.distributions.Distribution]`):
                A dictionary from `measurement` to torch distributions modelling the regression targets for each
                data element in each event. In particular, samples from these distributions will have shape
                `[batch_size, sequence_length, num_data_elements_per_event]`, such that `sample[i][j][k]` will
                correspond to a prediction for the regression target indexed by
                `batch['dynamic_indices'][i][j][k]`.
            `regression_labels` (`Dict[str, torch.FloatTensor]`):
                A dictionary from `measurement` to tensors of shape
                `[batch_size, sequence_length, num_data_elements_per_event]` containing regression targets for
                each data element, or 0 if that regression target is unobserved.
            `regression_indices` (`Dict[str, torch.LongTensor]`):
                A dictionary from `measurement` to tensors of shape
                `[batch_size, sequence_length, num_data_elements_per_event]` containing the integer index of
                the regression component observed in that position, or 0 if that regression target is
                unobserved. E.g., if we have 200 laboratory tests that we are regressing over, these indices
                state to which laboratory test results the values in `regression_labels` correspond.
        """
        if not valid_measurements: return {}, {}, {}, {}

        regression_loss_values = {}
        regression_dists       = {}
        regression_labels      = {}
        regression_indices     = {}
        for measurement in self.config.measurements_per_generative_mode[DataModality.MULTIVARIATE_REGRESSION]:
            if measurement not in valid_measurements: continue

            if event_type_mask_per_measurement is not None:
                event_mask = event_type_mask_per_measurement[measurement] & batch['event_mask']
            else:
                event_mask = batch['event_mask']

            measurement_idx = self.config.measurements_idxmap[measurement]
            vocab_start = self.config.vocab_offsets_by_measurement[measurement]

            # TODO(mmd): If we wanted, we could have `indices_measured_or_zero` reflect just the former part
            # of this `&`, and thus have predictions on all indices, even for those we don't observe values
            # for, but for now this functionality is not required, so we standardize them.
            tensor_idx = (
                (batch['dynamic_measurement_indices'] == measurement_idx) & batch['dynamic_values_mask']
            )

            indices_measured_or_zero = torch.where(
                tensor_idx,
                batch['dynamic_indices'] - vocab_start,
                torch.zeros_like(batch['dynamic_indices']),
            ).long()

            regr_dist = self.regression_layers[measurement](
                X=encoded, I=(None if is_generation else indices_measured_or_zero)
            )

            values_observed_or_zero = torch.where(
                tensor_idx,
                batch['dynamic_values'],
                torch.zeros_like(batch['dynamic_values']),
            ).float()

            # We don't need to shift here, as given this is a structured model, we'll always rely on elements
            # of the dependency graph that don't include these inputs to predict them (e.g., predict the
            # contents of the event given the time at which the event occurred).

            # TODO(mmd): Use NLL-\beta?
            if is_generation:
                loss_overall = None
            else:
                loss_per_label    = -regr_dist.log_prob(values_observed_or_zero)
                loss_per_event, _ = safe_weighted_avg(loss_per_label, tensor_idx)

                events_with_label = event_mask & tensor_idx.any(dim=-1)
                loss_overall = weighted_loss(loss_per_event, events_with_label)

            regression_loss_values[measurement] = loss_overall
            regression_dists[measurement]       = regr_dist
            regression_labels[measurement]      = values_observed_or_zero
            regression_indices[measurement]     = indices_measured_or_zero

        return (
            regression_loss_values,
            regression_dists,
            None if is_generation else regression_labels,
            None if is_generation else regression_indices,
        )

    def get_event_type_mask_per_measurement(
        self, batch: EventStreamPytorchBatch
    ) -> Dict[str, Optional[torch.BoolTensor]]:
        if self.config.event_types_per_measurement is None: return None

        event_type_mask = (
            batch['dynamic_measurement_indices'] == self.config.measurements_idxmap['event_type']
        )

        batch_event_type_indices = torch.where(
            event_type_mask,
            batch['dynamic_indices'] - self.config.vocab_offsets_by_measurement['event_type'],
            -1
        )

        out_masks = {}
        for measurement, valid_event_types in self.config.event_types_per_measurement.items():
            valid_event_types = self.config.event_types_per_measurement[measurement]
            valid_event_type_indices = {self.config.event_types_idxmap[et] for et in valid_event_types}

            # We only want to predict for events that are of the correct type.
            out_masks[measurement] = torch.any(
                torch.stack([(batch_event_type_indices == i) for i in valid_event_type_indices], 0),
                dim=0
            ).any(-1)
        return out_masks

    def forward(
            self, batch: EventStreamPytorchBatch, encoded: torch.FloatTensor, is_generation: bool = False,
    ) -> EventStreamTransformerForGenerativeSequenceModelOutput:
        # encoded is of one of two shapes:
        #   1. (batch size, sequence length, config.hidden_size), in the case that
        #      self.config.structured_event_processing_mode == 'conditionally_independent'.
        #   2. (batch size, sequence length, dependency graph len, config.hidden_size), in the case that
        #      self.config.structured_event_processing_mode != 'conditionally_independent'. In this case, the
        #      last element of the dependency graph is always the whole-event embedding, and the first element
        #      of the dependency graph is always assumed to be the time of the event.

        # These are the containers we'll use to process the outputs
        classification_dists_by_measurement = {}
        classification_losses_by_measurement = None if is_generation else {}
        classification_labels_by_measurement = None if is_generation else {}
        regression_dists       = {}
        regression_loss_values = None if is_generation else {}
        regression_labels      = None if is_generation else {}
        regression_indices     = None if is_generation else {}

        classification_measurements = set(self.classification_mode_per_measurement.keys())
        regression_measurements = set(
            self.config.measurements_per_generative_mode[DataModality.MULTIVARIATE_REGRESSION]
        )

        event_type_mask_per_measurement = self.get_event_type_mask_per_measurement(batch)

        if self.config.structured_event_processing_mode == 'conditionally_independent':
            bsz, seq_len, _ = encoded.shape
            whole_event_encoded = encoded

            # In this case, the whole_event_encoded representation actually is used to predict the next
            # event's contents, so we need to shift it to be in the right form for predicting things. In
            # particular, we prepend a vector of zeros to be used to predict the contents of the first event
            # (excluding the TTE of the first event which is guaranteed to be zero) and we _don't_ predict the
            # contents of the event after the end of this sequence (as we have no way to judge them). This
            # plan may bite us during generation, but it preserves the API between the structured and
            # non-structured versions, where the latter doesn't have any way at all to generate the contents
            # of the next event after the end of the sequence, as it needs a timepoint embedding to process
            # that prediction task.

            for_event_contents_prediction = torch.cat((
                torch.zeros_like(whole_event_encoded[:, 0, :]).unsqueeze(1), whole_event_encoded[:, :-1, :]
            ), dim=1)

            classification_out = self.get_classification_outputs(
                batch, for_event_contents_prediction, classification_measurements,
                event_type_mask_per_measurement=event_type_mask_per_measurement
            )
            classification_dists_by_measurement.update(classification_out[1])
            if not is_generation:
                classification_losses_by_measurement.update(classification_out[0])
                classification_labels_by_measurement.update(classification_out[2])

            regression_out = self.get_regression_outputs(
                batch, for_event_contents_prediction, regression_measurements, is_generation=is_generation,
                event_type_mask_per_measurement=event_type_mask_per_measurement
            )
            regression_dists.update(regression_out[1])
            if not is_generation:
                regression_loss_values.update(regression_out[0])
                regression_labels.update(regression_out[2])
                regression_indices.update(regression_out[3])

        else:
            bsz, seq_len, dep_graph_len, _ = encoded.shape
            whole_event_encoded = encoded[:, :, -1, :]

            # Now we need to walk through the other elements of the dependency graph (omitting the first
            # entry, which reflects time-only dependent values and so is covered by predicting TTE).
            for i in range(1, dep_graph_len):
                # In this case, unlike above, this level of the dependency graph is presumed to be used to
                # predict the data types listed in `self.config.measurements_per_dep_graph_level`, so we don't
                # need to shift anything as we did in the conditionally_independent case.
                dep_graph_level_encoded = encoded[:, :, i-1, :]
                # dep_graph_level_encoded is of shape (batch size, sequence length, hidden size)

                if self.config.measurements_per_dep_graph_level is None:
                    # TODO(mmd): This isn't quite right.

                    # If unspecified, we assume it contains all of them.
                    classification_measurements_in_level = classification_measurements
                    regression_measurements_in_level = regression_measurements
                else:
                    categorical_measurements_in_level = set()
                    numerical_measurements_in_level = set()
                    for measurement in self.config.measurements_per_dep_graph_level[i]:
                        if type(measurement) is tuple: measurement, mode = measurement
                        else: mode = MeasIndexGroupOptions.CATEGORICAL_AND_NUMERICAL

                        match mode:
                            case MeasIndexGroupOptions.CATEGORICAL_AND_NUMERICAL:
                                categorical_measurements_in_level.add(measurement)
                                numerical_measurements_in_level.add(measurement)
                            case MeasIndexGroupOptions.CATEGORICAL_ONLY:
                                categorical_measurements_in_level.add(measurement)
                            case MeasIndexGroupOptions.NUMERICAL_ONLY:
                                numerical_measurements_in_level.add(measurement)
                            case _:
                                raise ValueError(f"Unknown mode {mode}")

                    classification_measurements_in_level = categorical_measurements_in_level.intersection(
                        classification_measurements
                    )
                    regression_measurements_in_level = numerical_measurements_in_level.intersection(
                        regression_measurements
                    )

                classification_out = self.get_classification_outputs(
                    batch, dep_graph_level_encoded, classification_measurements_in_level,
                    event_type_mask_per_measurement=event_type_mask_per_measurement
                )
                classification_dists_by_measurement.update(classification_out[1])
                if not is_generation:
                    classification_losses_by_measurement.update(classification_out[0])
                    classification_labels_by_measurement.update(classification_out[2])

                regression_out = self.get_regression_outputs(
                    batch, dep_graph_level_encoded, regression_measurements_in_level,
                    is_generation=is_generation,
                    event_type_mask_per_measurement=event_type_mask_per_measurement,
                )
                regression_dists.update(regression_out[1])
                if not is_generation:
                    regression_loss_values.update(regression_out[0])
                    regression_labels.update(regression_out[2])
                    regression_indices.update(regression_out[3])

        # `whole_event_encoded` is of shape (batch size, sequence length, hidden size)
        TTE_LL_overall, TTE_dist, TTE_true = self.get_TTE_outputs(batch, whole_event_encoded)

        return EventStreamTransformerForGenerativeSequenceModelOutput(**{
            'loss': (
                sum(classification_losses_by_measurement.values()) + sum(regression_loss_values.values()) -
                TTE_LL_overall
            ) if not is_generation else None,
            'losses': GenerativeSequenceModelLosses(**{
                'classification': classification_losses_by_measurement,
                'regression': regression_loss_values,
                'time_to_event': None if is_generation else -TTE_LL_overall,
            }),
            'preds': GenerativeSequenceModelPredictions(
                classification = classification_dists_by_measurement,
                regression = regression_dists,
                regression_indices = regression_indices,
                time_to_event = TTE_dist,
            ),
            'labels': GenerativeSequenceModelLabels(
                classification = classification_labels_by_measurement,
                regression = regression_labels,
                regression_indices = regression_indices,
                time_to_event = None if is_generation else TTE_true
            ),
            'event_type_mask_per_measurement': event_type_mask_per_measurement,
            'event_mask': batch['event_mask'],
            'dynamic_values_mask': batch['dynamic_values_mask'],
        })

class StructuredEventStreamTransformerForGenerativeSequenceModeling(
    StructuredEventStreamGenerationMixin, StructuredEventStreamTransformerPreTrainedModel
):
    def __init__(
            self,
            config: StructuredEventStreamTransformerConfig,
        ):
        super().__init__(config)

        self.encoder = StructuredEventStreamTransformer(config)
        self.output_layer = StructuredEventStreamGenerativeOutputLayer(config)

        # Initialize weights and apply final processing
        self.post_init()

    def forward(
        self,
        batch: EventStreamPytorchBatch,
        is_generation: bool = False,
        **kwargs
    ):
        encoded = self.encoder(batch, **kwargs).last_hidden_state
        return self.output_layer(batch, encoded, is_generation=is_generation)

class StructuredEventStreamTransformerForStreamClassification(StructuredEventStreamTransformerPreTrainedModel):
    def __init__(
            self,
            config: StructuredEventStreamTransformerConfig,
        ):
        super().__init__(config)

        self.task = config.finetuning_task
        self.encoder = StructuredEventStreamTransformer(config)

        self.pooling_method = config.task_specific_params['pooling_method']

        is_binary = (config.id2label == {0: False, 1: True})
        if is_binary:
            assert config.num_labels == 2
            self.logit_layer = torch.nn.Linear(config.hidden_size, 1)
            self.criteria = torch.nn.BCEWithLogitsLoss()
        else:
            self.logit_layer = torch.nn.Linear(config.hidden_size, config.num_labels)
            self.criteria = torch.nn.CrossEntropyLoss()

        # Initialize weights and apply final processing
        self.post_init()

    @property
    def uses_dep_graph(self):
        return self.config.structured_event_processing_mode == StructuredEventProcessingMode.NESTED_ATTENTION

    def forward(
        self,
        batch: EventStreamPytorchBatch,
        **kwargs
    ):
        encoded = self.encoder(batch, **kwargs).last_hidden_state
        event_encoded = encoded[:, :, -1, :] if self.uses_dep_graph else encoded

        # `event_encoded` is of shape [batch X seq X hidden_dim]. For pooling, I want to put the sequence
        # dimension as last, so we'll transpose.
        event_encoded = event_encoded.transpose(1, 2)

        match self.pooling_method:
            case 'cls': stream_encoded = event_encoded[:, :, 0]
            case 'last': stream_encoded = event_encoded[:, :, -1]
            case 'max': stream_encoded = safe_masked_max(event_encoded, batch['event_mask'])
            case 'mean': stream_encoded, _ = safe_weighted_avg(event_encoded, batch['event_mask'])
            case _: raise ValueError(f"{self.pooling_method} is not a supported pooling method.")

        logits = self.logit_layer(stream_encoded).squeeze(-1)
        labels = batch['stream_labels'][self.task]
        loss = self.criteria(logits, labels)

        return EventStreamTransformerForStreamClassificationModelOutput(
            loss=loss, preds=logits, labels=labels,
        )
