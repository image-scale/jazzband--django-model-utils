from __future__ import annotations

from typing import Any, Generic, Type, TypeVar

from django.db import connection, models
from django.db.models import Q
from django.db.models.query import QuerySet

ModelT = TypeVar("ModelT", bound=models.Model, covariant=True)


class InheritanceIterable:
    """Custom iterable that converts rows to subclass instances."""

    def __init__(self, queryset: InheritanceQuerySet[Any]) -> None:
        self.queryset = queryset

    def __iter__(self) -> Any:
        for obj in self.queryset._iterable_class(self.queryset):  # type: ignore
            yield self.queryset._get_subclass_instance(obj)


class InheritanceQuerySet(QuerySet[ModelT]):
    """QuerySet that supports select_subclasses() and get_subclass()."""

    subclasses: list[str]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.subclasses: list[str] = []

    def _clone(self) -> InheritanceQuerySet[ModelT]:
        c = super()._clone()  # type: ignore
        c.subclasses = self.subclasses[:]
        return c

    def select_subclasses(self, *subclasses: str | Type[models.Model]) -> InheritanceQuerySet[ModelT]:
        """Select related subclass models for downcast."""
        new_qs = self._clone()

        if not subclasses:
            # Select all subclasses
            related_names = self._get_all_subclass_related_names()
        else:
            related_names = []
            for subclass in subclasses:
                if isinstance(subclass, str):
                    # Validate it's in the discovered subclasses
                    all_related = self._get_all_subclass_related_names()
                    if subclass not in all_related:
                        raise ValueError(
                            f"'{subclass}' is not in the discovered subclasses, tried: {all_related}"
                        )
                    related_names.append(subclass)
                else:
                    # It's a model class - convert to related name
                    related_name = self._get_related_name_for_model(subclass)
                    if related_name is None:
                        if subclass == self.model:
                            continue  # Selecting self is a no-op
                        raise ValueError(f"'{subclass}' is not a subclass of {self.model}")
                    related_names.append(related_name)

        # De-duplicate while preserving order
        seen = set()
        unique_names = []
        for name in related_names:
            if name not in seen:
                seen.add(name)
                unique_names.append(name)

        new_qs.subclasses = unique_names
        if unique_names:
            new_qs = new_qs.select_related(*unique_names)
        return new_qs

    def _get_all_subclass_related_names(self) -> list[str]:
        """Get all related names for subclasses."""
        related_names = []
        self._collect_subclass_related_names(self.model, '', related_names)
        return related_names

    def _collect_subclass_related_names(self, model: type, prefix: str, result: list[str]) -> None:
        """Recursively collect related names for all subclasses."""
        opts = model._meta
        for rel in opts.related_objects:
            if not rel.one_to_one:
                continue
            # Check parent_link on the relation object itself
            if not getattr(rel, 'parent_link', False):
                continue
            related_model = rel.related_model
            related_name = rel.get_accessor_name()
            full_name = f"{prefix}{related_name}" if prefix else related_name
            result.append(full_name)
            # Recurse into grandchildren
            self._collect_subclass_related_names(related_model, full_name + '__', result)

    def _get_related_name_for_model(self, model: type) -> str | None:
        """Get the related name path to reach a model class."""
        return self._find_related_name(self.model, model, '')

    def _find_related_name(self, current: type, target: type, prefix: str) -> str | None:
        """Recursively find the related name path to a target model."""
        opts = current._meta
        for rel in opts.related_objects:
            if not rel.one_to_one:
                continue
            # Check parent_link on the relation object itself
            if not getattr(rel, 'parent_link', False):
                continue
            related_model = rel.related_model
            related_name = rel.get_accessor_name()
            full_name = f"{prefix}{related_name}" if prefix else related_name
            if related_model == target:
                return full_name
            # Check grandchildren
            found = self._find_related_name(related_model, target, full_name + '__')
            if found:
                return found
        return None

    def get_subclass(self, *args: Any, **kwargs: Any) -> ModelT:
        """Get an object and return it as its proper subclass instance."""
        return self.select_subclasses().get(*args, **kwargs)

    def instance_of(self, *models_list: Type[models.Model]) -> InheritanceQuerySet[ModelT]:
        """Filter to instances of specific subclasses."""
        new_qs = self._clone()
        q = Q()
        for model in models_list:
            if model == self.model:
                continue
            related_name = self._get_related_name_for_model(model)
            if related_name:
                # Filter using the parent link field
                parts = related_name.split('__')
                final_part = parts[-1]
                # Get the pk field name for the related model
                rel_opts = model._meta
                pk_col = rel_opts.pk.column
                lookup = f"{related_name}__{pk_col}__isnull"
                q |= Q(**{lookup: False})
        new_qs = new_qs.filter(q)
        return new_qs

    def _get_subclass_instance(self, obj: Any) -> Any:
        """Convert an object to its proper subclass instance."""
        if not isinstance(obj, models.Model):
            return obj

        # Sort subclasses by depth (deepest first) to get the most specific type
        # e.g., 'child1__grandchild1' should be checked before 'child1'
        sorted_subclasses = sorted(self.subclasses, key=lambda x: -x.count('__'))

        best_match = obj
        best_depth = 0

        for subclass_name in sorted_subclasses:
            current = obj
            parts = subclass_name.split('__')
            depth = len(parts)
            for part in parts:
                try:
                    current = getattr(current, part, None)
                    if current is None:
                        break
                except Exception:
                    current = None
                    break
            if current is not None and isinstance(current, models.Model):
                # Found a valid subclass - use if it's deeper than current best
                if depth > best_depth:
                    best_match = current
                    best_depth = depth

        # Copy extras from the original object to best match
        if best_match is not obj and hasattr(obj, '__dict__'):
            for key, value in obj.__dict__.items():
                if not key.startswith('_') and not hasattr(best_match, key):
                    setattr(best_match, key, value)

        return best_match

    def iterator(self, chunk_size: int | None = None) -> Any:
        """Return an iterator over the results."""
        if chunk_size is not None:
            # Request chunked cursor
            for obj in super().iterator(chunk_size=chunk_size):
                yield self._get_subclass_instance(obj)
        else:
            for obj in super().iterator():
                yield self._get_subclass_instance(obj)

    def __iter__(self) -> Any:
        """Iterate over the results, converting to subclass instances."""
        for obj in super().__iter__():
            yield self._get_subclass_instance(obj)


class InheritanceManager(models.Manager[ModelT]):
    """Manager that supports select_subclasses() and get_subclass()."""

    _queryset_class = InheritanceQuerySet

    def get_queryset(self) -> InheritanceQuerySet[ModelT]:
        return self._queryset_class(self.model, using=self._db)

    def select_subclasses(self, *subclasses: str | Type[models.Model]) -> InheritanceQuerySet[ModelT]:
        return self.get_queryset().select_subclasses(*subclasses)

    def get_subclass(self, *args: Any, **kwargs: Any) -> ModelT:
        return self.get_queryset().get_subclass(*args, **kwargs)

    def instance_of(self, *models_list: Type[models.Model]) -> InheritanceQuerySet[ModelT]:
        return self.get_queryset().instance_of(*models_list)


class QueryManager(models.Manager[ModelT]):
    """Manager that filters by default kwargs or Q objects."""

    def __init__(self, *args: Q, **kwargs: Any) -> None:
        self._args = args
        self._kwargs = kwargs
        self._order_by: tuple[str, ...] = ()
        super().__init__()

    def order_by(self, *args: str) -> QueryManager[ModelT]:
        """Return a new manager with specified ordering."""
        new = QueryManager(*self._args, **self._kwargs)
        new._order_by = args
        return new

    def get_queryset(self) -> QuerySet[ModelT]:
        qs = super().get_queryset()
        if self._args:
            q = self._args[0]
            for arg in self._args[1:]:
                q = q & arg
            qs = qs.filter(q)
        if self._kwargs:
            qs = qs.filter(**self._kwargs)
        if self._order_by:
            qs = qs.order_by(*self._order_by)
        return qs


class SoftDeletableQuerySetMixin(Generic[ModelT]):
    """Mixin that provides soft delete functionality to QuerySets."""

    def delete(self: QuerySet[ModelT]) -> tuple[int, dict[str, int]]:  # type: ignore
        """Soft delete all objects in the queryset."""
        # Get count before update
        count = self.count()
        if count == 0:
            return (0, {})
        # Get the model label
        model_label = self.model._meta.label
        # Perform the update
        self.update(is_removed=True)
        return (count, {model_label: count})


class SoftDeletableQuerySet(SoftDeletableQuerySetMixin[ModelT], QuerySet[ModelT]):
    """QuerySet that provides soft delete functionality."""
    pass


class SoftDeletableManagerMixin(Generic[ModelT]):
    """Mixin that provides soft delete filtering."""

    def get_queryset(self: models.Manager[ModelT]) -> SoftDeletableQuerySet[ModelT]:  # type: ignore
        # Use SoftDeletableQuerySet and filter out removed items
        return SoftDeletableQuerySet(self.model, using=self._db).filter(is_removed=False)


class SoftDeletableManager(SoftDeletableManagerMixin[ModelT], models.Manager[ModelT]):
    """Manager that filters out soft-deleted objects."""

    _queryset_class = SoftDeletableQuerySet

    def get_queryset(self) -> SoftDeletableQuerySet[ModelT]:
        return self._queryset_class(self.model, using=self._db).filter(is_removed=False)


class JoinQueryset(QuerySet[ModelT]):
    """QuerySet that supports self-joins."""

    def join(self, qs: QuerySet[Any] | None = None) -> JoinQueryset[ModelT]:
        """Filter this queryset based on another queryset using a join."""
        if qs is None:
            # Self-join - just return a clone
            return self._clone()  # type: ignore

        # Find the relationship between the two models
        other_model = qs.model
        join_field = None

        # Check for FK from other to self
        for field in other_model._meta.fields:
            if hasattr(field, 'related_model') and field.related_model == self.model:
                join_field = field
                break

        # Check for FK from self to other
        if join_field is None:
            for field in self.model._meta.fields:
                if hasattr(field, 'related_model') and field.related_model == other_model:
                    join_field = field
                    break

        if join_field is None:
            # No direct relationship found, return all
            return self._clone()  # type: ignore

        if join_field.model == other_model:
            # FK from other to self - filter self by related items
            related_name = join_field.name
            # Get pks from the other queryset's FK values
            pk_field = f'{related_name}_id'
            other_pks = set(qs.values_list(pk_field, flat=True))
            return self.filter(pk__in=other_pks)  # type: ignore
        else:
            # FK from self to other
            related_name = join_field.name
            other_pks = set(qs.values_list('pk', flat=True))
            return self.filter(**{f'{related_name}__pk__in': other_pks})  # type: ignore

    def _clone(self) -> JoinQueryset[ModelT]:
        c = super()._clone()  # type: ignore
        return c
