# EventStreamData 
The `EventStreamData` module contains code for representing and using Event-stream datasets, designed for
three applications, in priority order:
    1. For use in generative point-processes / generative sequence models in PyTorch using
       `EventStreamTransformer`.
    2. For general PyTorch deep-learning applications and models.
    3. For general modeling and data analysis.

There are several classes and functions in this module that may be useful, which we document below.

## Overall Data Model
We assume any "event stream dataset" consists of 3 fundamental data units:
  1. Subjects, who can have many events or measurements, and are identified uniquely by `subject_id`.
  2. Events, which occur in a continuous-time manner and can have many measurements, but uniquely belong to a
     single subject, and are identified uniquely by `event_id` and are characterized by a categorical
     `event_type`.
  3. Measurements, which can be either static (and linked to subjects), dynamic (and linked to events), or can
     be pre-specified, fixed functions of static variables and time, and can be computed in the system on the
     fly from a functional form. In terms of a data model, however, as these are only dependent on time, such
     a measurement can only take on a single value per event. Measurements are characterized by many
     properties and can be pre-processed automatically by the system in several ways.

This data model is realized explicitly in the internal structure of the `EventStreamDataset` class, documented
below.

Note that currently, at some places in the code, measurements are referred to as `metadata` --- this will be
removed eventually.
TODO(mmd): Fully switch over the name.

This data model translate during pytorch embedding to one in which data are presented to models in a sparse,
fully-temporally realized format which should be more efficient computationally than a dense format and based
on [existing literature](https://arxiv.org/abs/2106.11959) is likely to be a high-performance style of data
embedding overall (at least with respect to the per-timestamp data embedding practices).

## Measurements
Measurements are identified uniquely by a single column name or, in the context of a multivariate regression 
measurement, by a pair of column names defining the keys and values, respectively. Ultimately, these names 
are linked to columns in various internal dataframes. Measurements can be broken down into several categories
along different axes:

### Temporality
As stated above, measurements can take on one of the following three modes relating to how they vary in time:
  1. `STATIC`: in which case they are unchanging and can be linked uniquely to a subject.
  2. `FUNCTIONAL_TIME_DEPENDENT`: in which case they can be specified in functional form dependent only on
     static subject data and/or a continuous timestamp.
  2. `DYNAMIC`: in which case they are time-varying, but the manner of this variation cannot be specified in a
     static functional form as in the case of `FUNCTIONAL_TIME_DEPENDENT`. Accordingly, these measurements are
     linked to events in a many to one fashion and are identified via a separate, `metadata_id` identifier.

### Measurement Observation Data Modality
Measurements can also vary in what modality (e.g., continuous/numeric valued, categorical, etc.) their
observations are. These definitions of modality are both motivated by three things:
  1. How we need to pre-process the data (e.g., categorical metadata need to be mapped to a vocabulary, and
     continuous metadata need to undergo outlier detection and normalization).
  2. How we should embed the data for deep-learning applications (e.g., we need to use embedding matrices for
     categorical values and multiply by continuous values for numerical values).
  3. How one would go about generating such data within a generative model (e.g., is this measurement a
     multi-label or single-label categorical value per event? Is it a partially or fully observed regression
     task? etc.).

In particular, we recognize the following kinds of measurement modalities currently:
  1. `SINGLE_LABEL_CLASSIFICATION`: In this case, the measure is assumed to take on a unique label from a
     static collection of options, and to only be observed once per event/subject at maximum.
  2. `MULTI_LABEL_CLASSIFICATION`: In this case, the measure is assumed to take on zero or more unique labels
     from a static collection of options per event. Currently, this modality is only supported on `DYNAMIC`
     measurements.
  3. `MULTIVARIATE_REGRESSION`: In this case, the measure is assumed to be presented in a sparse, multivariate
     format via key-value pairs, where the keys correspond to the dimensions of the multivariate regression
     problem and the values the values observed. This keys for this measure are assumed to be observed in a
     multilabel manner per event (this modality is currently only supported for `DYNAMIC` measurements), with
     the values then being observed in a continuous sense conditional on keys being measured for that event.
  4. `UNIVARIATE_REGRESSION`: In this case, the measure is assumed to contain only a single continuous value,
     which may or may not be fully observed. This modality is only currently supported for `STATIC` OR
     `FUNCTIONAL_TIME_DEPENDENT` measurements.

Numerical measurements may also be associated with measurement metadata, represnted via pandas dataframes or
series describing the measure, including things such as units, outlier/censoring bounds, and (after
pre-processing) outlier detection models or normalizers that have been fit on the training data.

Numerical measurements, during pre-processing, may have their values (in a key-dependent manner, in the case
of `MULTIVARIATE_REGRESSION` measurements)  be further subtyped into the following categories, which dictate
the pre-processing of thsoe values:
  1. `INTEGER`: the observed values will be converted to integers.
  2. `FLOAT`: the observed values will remain as floating point values.
  3. `CATEGORICAL_INTEGER`: the observed values will be converted to integers, then normalized into a set of
     categories on the basis of the values observed.
  4. `CATEGORICAL_FLOAT`: the observed values will be normalized into a set of categories on the basis of the
     values observed.

### Configuration: `MeasurementConfig`
Measurements can be configured via the `MeasurementConfig` object (inside `config.py`). At initialization,
this configuration object defines the metrics an `EventStreamDataset` object should pre-process for modelling
use, but it also is filled during pre-processing with information about that measurement in the data.

A subset of notable fields include:
  1. `MeasurementConfig.name`: contains the name of the measurement, and links to columns in the data. This
     does not need to be set manually in practice; it will be filled on the basis of how the measurment config
     is stored inside the broader `EventStreamDatasetConfig`.
  2. `modality` tracks the observation modalities discussed above, and `temporality` the temporality modes
     discussed above. Both use `StrEnum`s for storage, which means in practice either their enum forms (e.g.,
     `DataModality.UNIVARIATE_REGRESSION`) or lowercase strings of the enum variable name (e.g.,
     `'univariate_regression'`) can be used.
  3. For numerical measurements, `values_column` stores the associated values for a key-value paired
     `MULTIVARIATE_REGRESSION` measurement (with the keys stored in the column of the same name as `name`) and
     `measurement_metadata` stores associated metadata about the measurement, such as units, outlier detection
     models, etc.
  4. For `FUNCTIONAL_TIME_DEPENDENT` models, `functor` stores the function (stored as an object that is
     translatable to a plain-old-data dictionary and vice-versa) that is used to compute the column value from
     the static data and the timestamps. While user-defined functors can be used (though to do so they must be
     added to the `MeasurementConfig.FUNCTORS` class dictionary), currently, only two functors are pre-built
     for use:
    1. `AgeFunctor`, which takes as input a "date of birth" column name within the static data and computes
       the difference, in units of 365 days, between the event timestamps and that subject's date of birth.
    2. `TimeOfDayFunctor`, which takes no inputs and returns a string categorizing the time of day of the
       event timestamp into one of 4 buckets.
  5. For all measures except for `UNIVARIATE_REGRESSION` measurements, the `vocabulary` member stores a
     `Vocabulary` object which maintains a vocabulary of the observed categorical values (or keys for
     `MULTIVARIATE_REGRESSION` metrics) that have been observed. All vocabularies begin with an `UNK` token
     and subsequently proceed in descending order of observation frequency. Observation frequency is also
     stored, and vocabularies can be filtered to only elements occurring sufficiently frequently via a
     function and "idxmaps" (maps from vocabulary elements to their integer index) are also available via an
     accessor. These can be built from observations during pre-processing dynamically.
  6. `present_in_event_types` stores which for which types of events this measurement can be observed. This is
     only valid for `DYNAMIC` measurements, as `STATIC` measurements are not associated with events and
     `FUNCTIONAL_TIME_DEPENDENT` measurements are only dependent on timestamps among event variables, so can
     exist for all events.
  6. `observation_frequency` stores how frequently that measurement was observed with a non-null value (or a
     non-null key in the case of `MULTIVARIATE_REGRESSION` measurements) among all possible instances it could
     have been observed (e.g., all possible subjects for `STATIC` measurements, or otherwise all possible
     events of the valid type).

This configuration file is readable from and writable to JSON files. Full details of the options for this
configuration object can be found in its source documentation.

## `EventStreamDataset`
This class stores an event stream dataset and performs pre-processing as dictated by a configuration object.

### Configuration via `EventStreamDatasetConfig`
This configuration object stores three classes of parameters:
  1. A dictionary from column names to `MeasurementConfig` objects. At initialization, the names of the
     configuration objects are set to be equal to the keys in this dictionary.
  2. A variety of preprocessing simple parameters, which dictate things like how frequently a measurement must
     be observed to be included in the training dataset, vocabulary element cutoffs, numerical to categorical
     value conversion parameters, etc.
  3. Two dictionaries thad define what class should be used and how that class should be parametrized for
     performing outlier detection and normalization. These configuration dictionaries, if specified, must
     contain a `'cls'` key _which further must link to an option in the `EventStreamDataset.METADATA_MODELS`
     class dictionary_.

This configuration file is readable from and writable to JSON files. Full details of the options for this
configuration object can be found in its source documentation.

### Construction
One can construct an `EventStreamDataset` in several ways. In all cases, one must present an
`EventStreamDatasetConfig` object to configure the dataset, and a pandas DataFrame, `events_df` which contains
the underlying events of these data. This dataframe must contain an column named `'subject_id'` and another
named `'timestamp'`. However, there are several other options that are not mandatory:
  1. The `events_df` dataframe can contain measurements directly within it even in a
     one-event-to-many-measurements format, via a column `metadata` which contains a set of `ExpandableDfDict`
     objects (which are functionally pandas dataframes stored as dictionaries). If this form is used, these
     dictionaries will be expanded within the class into a separate dataframe for more efficient
     pre-processing. Alternatively, dynamic, per-event measurements can be specified directly in a
     `metadata_df` dataframe, which must contain a column `event_id` that links to the index of `events_df`.
     Note that, upon construction, the `events_df` passed in will be copied and sorted by `subject_id` then
     `timestamp`.
  2. One can specify static data per-subject via a `subjects_df` dataframe, which must contain a unique index
     named `subject_id` but is otherwise unconstrainted. If this dataframe is unspecified, any passed static
     measurements in the configuration object will cause an error at pre-processing time.

### Pre-processing Data Capabilities
This dataset can pre-process the code in several key ways, listed and described below.

#### Data splitting
The system can automatically split the underlying data by subject into random subsets (in a seedable manner)
via user-specified ratios. These splits can be named, and if three splits are provided (or two splits whose
ratios do not sum to one, in which case a third is inferred, the names `'train'`, `'tuning'`, and `'held_out'`
are inferred for the passed ratios in that order. These three names are special, and sentinel accessors exist
in the code to extract only events in the training set, etc. 

Note that the seeds used for this function, and aseeds used anywhere throughout this code, are stored within
the object, even if not specified by the user, so calculations can always be re-covered stably.

#### Pre-process numerical data elements
The system can automatically filter out and/or censor outliers based on pre-specified cutoff values per data
column and key identifier, fit learned outlier detection models over the data, and fit normalizer models over
the data. It also can recognize when numerical data actually loks like it should be treated as a one-hot
categorical data element.

This applies both to static and dynamic data elements.

#### Pre-process categorical data elements
The system can fit vocabularies to categorical columns and filter out elements that happen insufficiently
frequently.

This applies both to static and dynamic data elements.

#### Pre-compute and pre-process strictly time-dependent feature functions
Some features are dynamic, but rather than being dictated by events in the data, they are determined on the
basis of a priori, known functions whose sole input is the time of the event. Some examples of this include
the time-of-day of the event (e.g., morning, evening, etc.), the subject's age as of the event, etc. 

The system contained here can pre-compute these time-dependent feature values, then apply the same
pre-processing capabilities to the appropriate column types to the results.

### Internal Storage
#### `EventStreamDataset.subjects_df`
This dataframe stores the _subjects_ that make up the data. It has a subject per row and has the following
mandatory schema elements:
  * A unique, integer index named `subject_id`.

It may have additional, user-defined schema elements that can be leveraged during dataset pre-processing for
use in modelling.

TODO(mmd): Convert categorical columns to categories during preprocessing to save space and downstream time.

#### `EventStreamDataset.events_df`
This dataframe stores the _events_ that make up the data. It has an event per row and has the following schema
elements:
  * A unique, integer index named `event_id`.
  * An integer column `subject_id` which is joinable against `subjects_df.index` and indicates to which
    subject a row's event corresponds. Many events may link to a single subject.
  * A pandas datetime column `timestamp` which tracks the time of the row's event.
  * A string column `event_type` which indicates what type of event the row's event is.

TODO(mmd): Make `event_type` a categorical column.

#### `EventStreamDataset.joint_metadata_df`
This dataframe stores the _metadata elements_ that describe each event in the data. It has a metadata element
per row and has the following mandatory schema elements:
  * A unique, integer index named `metadata_id`
  * An integer column `event_id`, which is joinable against `events_df.index` and indicates to which event a
    the row's metadata element corresponds. Many metadata elements may link to a single event.
  * An integer column `subject_id` which is joinable against `subjects_df.index` and indicates to which
    subject the row's metadata element corresponds. Many metadata elements may link to a single subject. Must
    be consistent with the implied index through `event_id`, but is pre-written for efficient downstream
    selection operations.
  * A string column `event_type` which indicates to which type of event the row's metadata element
    corresponds. Must be consistent with the implied mapping through `event_id`, but is pre-written for
    efficient downstream selection operations.

It may have additional, user-defined schema elements that can be leveraged during dataset pre-processing for
use in modelling.

TODO(mmd): Rename to `dynamic_measurements_df`.
TODO(mmd): Make `event_type` a categorical column.
TODO(mmd): Convert categorical columns to categories during preprocessing to save space and downstream time.

### Other views
You can also produce an `events_df_with_metadata` view which looks just like `events_df` but with an
additional `metadata` column which has the sub-dataframe corresponding to non-null columns in
`joint_metadata_df` for the rows corresponding to the event in question, re-organized as an `ExpandableDfDict`
object. This may be removed in the future, as it is not very useful and slow to construct.

## `EventStreamPytorchDataset`
This class converts an `EventStreamDataset` object into a pytorch deep-learning friendly dataset class. There
are three relevant data structures to understand here:
  1. That of how internal indexing and labels are specifiable for sequence classification applications.
  2. That of individual items returned from `__getitem__`
  3. That of batches produced by class instance's `collate` function.

### Task specification
When constructed by default, an `EventStreamPytorchDataset` takes only an `EventStreamDataset` object, a data
split (e.g., `'train'`, `'tuning'`, or `'held_out'`), and a
very lightweight configuration object with pytorch specific options. In this mode, it will have length given
by the number of subjects in the `EventStreamDataset` and will produce batches suitable for embedding and
downstream modelling over randomly chosen sub-sequences within each subject's data capped at the
config-specified maximum sequence length. This mode is useful for generative sequence modelling, but less so
for supervised learning, in which we need finer control over the subjects, ranges, and labels returned from
this class.

For these use cases, users can also specify a `task_df` object at construction of this dataset. This dataframe
must contain a `subject_id` column, a `start_time` column, and an `end_time` column. These define the cohort
and time-ranges over which this dataset will operate (limited to the associated split, of course), such that
the pytorch dataset will have length equal to the subset of `task_df` that represents the specified split
(even if subjects are repeated within `task_df`).

`task_df` may additionally have zero or more task label columns. The dataset will do minor pre-processing on
these columns for use, including inferring vocabularies for categorical or integral valued columns, such that
variables can be automatically filled on model configuration objects based on dataset specification.

### Per-item representation
The `__getitem__(i)` method, which returns the data element for patient `i`, returns dictionaries as follows.
Let us define the following variables:
  * Let `L` denote the sequence length for patient `i`.
  * Let `K` denote the number of static data elements observed for patient `i`.
  * Let `M[j]` denote the number of per-event data elements observed for patient `i`, event `j`.

```
{
  # Control variables
  # These aren't used directly in actual computation, but rather are used to define losses, positional
  # embeddings, dependency graph positions, etc.
  'time': [L],
  'event_type': [L],

  # Static Embedding Variables
  # These variables are static --- they are constant throughout the entire sequence of events.
  'static_indices': [K],
  'static_measurement_indices': [K],

  # Dynamic Embedding Variables
  # These variables are dynamic --- each event `j` has different values.
  'dynamic_indices': [L X M[j]], # (ragged)
  'dynamic_values': [L X M[j]], # (ragged)
  'dynamic_measurement_indices': [L X M[j]], # (ragged)
}
```

`static_data_values` and `data_values` in the above dictionary may contain `np.NaN` entries where values were
not observed with a given data element. All other data elements are fully observed. The elements correspond to
the following kinds of features:
  * `'static_*'` corresonds to features of the subject that are static over the duration of the sequence.
    E.g., in a medical dataset, a patient's polygenic risk score is unchanging throughout their life.
  * `'time'` corresponds to the number of minutes since the start of the day (in local time) of the first
    event of the sequence.
  * `'dynamic_*'` corresponds to event specific metadata elements describing each sequence event.
  * `'*_indices'` corresponds to the categorical index of the data element. E.g., in a medical dataset, the
    index of a particular laboratory test.
  * `'*_values'` corresponds to the numerical value associated with a data element. E.g., in a medical
    context, the value observed in a particular laboratory test.
  * `'*_measurement_indices'` corresponds to the identity of the governing measurement for a particular data
    element. E.g., in a medical dataset, this indicates that a data element is a laboratory test
    measurement at all.

If a `task_df` with associated task labels were also specified, then there will also be an entry in the output
dictionary per task label containing the task's label for that row in the dataframe as a single-element list.

### Batch representation: `EventStreamPytorchBatch`
The `collate` function takes a list of per-item representation and returns a batch representation. This final
batch representation can be accessed like a dictionary, but it is also a object stored in `types.py` of class
`EventStreamPytorchBatch`. It has some additional properties that can be useful, such as `batch_size`,
`sequence_length`, and `n_data_elements`.

The batch representation has the following structure. Let us define the following variables:
  * `B` is the batch size.
  * `L` is the per-batch maximum sequence length.
  * `M` is the per-batch maximum number of data elements per event.
  * `K` is the per-batch maximum number of static data elements.
```
EventStreamPytorchBatch(**{
  # Control variables
  # These aren't used directly in actual computation, but rather are used to define losses, positional
  # embeddings, dependency graph positions, etc.
  'time': [B X L], # (FloatTensor, normalized such that the first entry for each sequence is 0)
  'event_type': [B X L], # (LongTensor, 0 <=> no event was observed)

  'dynamic_values_mask': [B X K], # (BoolTensor, indicates whether a static data element was observed)

  # Static Embedding Variables
  # These variables are static --- they are constant throughout the entire sequence of events.
  'static_indices': [B X K], # (LongTensor, 0 <=> no static data element was observed)
  'static_measurement_indices': [B X K], # (FloatTensor, 0 <= no static data element was observed)

  # Dynamic Embedding Variables
  # These variables are dynamic per-event.
  'dynamic_indices': [B X L X M], # (LongTensor, 0 <=> no dynamic data element was observed)
  'dynamic_values': [B X L X M], # (FloatTensor, 0 <= no dynamic data element was observed)
  'dynamic_measurement_indices': [B X L X M], # (LongTensor, 0 <=> no data element was observed
})
```

If a `task_df` with associated task labels were also specified, then there will also be a dictionary at key
`stream_labels` within this output batch object that has keys given by task names and values given by collated
tensors of those task labels.

## Data Embedding: `DataEmbeddingLayer`
Once data are collated into a batch, they need to be usable in a pytorch deep learning model. Ultimatley, any
functional embedding strategy will produce a view that contains a fixed size representation of each event in
the sequence, with static data embedded either separately or combined with sequence elements in some form.

We have a module, `DataEmbeddingLayer`, which manages this process for you in a computationally efficient and
performant manner. It currently supports several embedding modes.
  1. Numerical and categorical embeddings can be computed via separate embedding matrices of differing
     dimensions, prior to being combined via projection layers and summed together in a weighted, normalized
     fashion.
  2. Static data and dynamic data can both be embedded, and combined either via summation across all events,
     concatenation across all events, or by prepending static embeddings to the beginning of each event
     sequence.
  3. Data can be embedded across all measurement types in a single output, or split into differing groups per
     measurement type and embedded separately per group, concatenated into a new dimension after the sequence
     dimension of the input tensors.
