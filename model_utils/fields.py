from __future__ import annotations

import re
import secrets
import uuid
from typing import Any, Callable, Generic, TypeVar

from django.db import models
from django.db.models import NOT_PROVIDED
from django.utils import timezone

T = TypeVar("T")


SPLIT_MARKER = '<!-- split -->'


def get_excerpt(content: str) -> str:
    """Extract excerpt from content, using <!-- split --> marker or auto-split."""
    if not content:
        return ""

    # Check for explicit split marker on its own line
    lines = content.split('\n')
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == SPLIT_MARKER:
            # Found marker on its own line
            excerpt = '\n'.join(lines[:i]).rstrip()
            if excerpt.endswith('\n'):
                return excerpt
            return excerpt

    # Check for split marker at start of a line (after paragraph break)
    pattern = r'\n' + re.escape(SPLIT_MARKER) + r'\s*\n'
    match = re.search(pattern, content)
    if match:
        return content[:match.start()]

    # Auto-split at second paragraph
    paragraphs = re.split(r'\n\s*\n', content)
    if len(paragraphs) > 2:
        return paragraphs[0] + '\n\n' + paragraphs[1]
    return content


class SplitText:
    """Wrapper for split text with excerpt and content properties."""

    def __init__(self, instance: models.Model | None, field: SplitField, content: str) -> None:
        self._instance = instance
        self._field = field
        self._content = content if content else ""

    def __str__(self) -> str:
        return self._content

    @property
    def content(self) -> str:
        return self._content

    @content.setter
    def content(self, value: str) -> None:
        self._content = value
        if self._instance:
            self._instance.__dict__[self._field.name] = value

    @property
    def excerpt(self) -> str:
        return get_excerpt(self._content)

    @property
    def has_more(self) -> bool:
        return SPLIT_MARKER in self._content


class SplitDescriptor(Generic[T]):
    """Descriptor for SplitField."""

    def __init__(self, field: SplitField) -> None:
        self.field = field

    def __get__(self, obj: models.Model | None, type: type | None = None) -> Any:
        if obj is None:
            raise AttributeError
        content = obj.__dict__.get(self.field.name, "")
        if isinstance(content, SplitText):
            return content
        return SplitText(obj, self.field, content)

    def __set__(self, obj: models.Model, value: Any) -> None:
        if isinstance(value, SplitText):
            obj.__dict__[self.field.name] = value._content
        else:
            obj.__dict__[self.field.name] = value


class SplitField(models.TextField):
    """A TextField that stores text with a split marker for excerpts."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    def contribute_to_class(self, cls: type, name: str, private_only: bool = False, **kwargs: Any) -> None:
        super().contribute_to_class(cls, name, private_only=private_only, **kwargs)
        setattr(cls, name, SplitDescriptor(self))
        # Add a hidden field to store excerpt
        excerpt_field = models.TextField(editable=False)
        excerpt_field_name = f'_{name}_excerpt'
        cls.add_to_class(excerpt_field_name, excerpt_field)

    def value_to_string(self, obj: models.Model) -> str:
        value = getattr(obj, self.name)
        if isinstance(value, SplitText):
            return value.content
        return str(value) if value else ""

    def pre_save(self, model_instance: models.Model, add: bool) -> Any:
        value = getattr(model_instance, self.name)
        if isinstance(value, SplitText):
            return value.content
        return value


class MonitorField(models.DateTimeField):
    """A DateTimeField that monitors another field for changes."""

    def __init__(self, monitor: str, when: list[Any] | None = None, **kwargs: Any) -> None:
        self.monitor = monitor
        self.when = when
        # If null is allowed, don't set a default
        if kwargs.get('null', False):
            kwargs.setdefault('default', None)
        else:
            kwargs.setdefault('default', timezone.now)
        kwargs.setdefault('editable', False)
        super().__init__(**kwargs)

    def contribute_to_class(self, cls: type, name: str, private_only: bool = False, **kwargs: Any) -> None:
        super().contribute_to_class(cls, name, private_only=private_only, **kwargs)
        models.signals.pre_save.connect(self._check_monitor, sender=cls)

    def _check_monitor(self, sender: type, instance: models.Model, raw: bool = False, **kwargs: Any) -> None:
        if raw:
            return

        # Get the current value of monitored field
        current_value = getattr(instance, self.monitor)

        # Handle the when=[] case (never update)
        if self.when is not None and len(self.when) == 0:
            return

        # Check if this is a new object
        if instance.pk is None:
            # New object - set if value matches when condition
            if self.null and getattr(instance, self.attname) is None:
                if self.when is None or current_value in self.when:
                    setattr(instance, self.attname, timezone.now())
            return

        # Existing object - check if the monitored field has changed
        update_fields = kwargs.get('update_fields')
        if update_fields is not None and self.monitor not in update_fields:
            # The monitored field is not being updated
            return

        # Get the previous value from the database
        try:
            # Check if the monitored field is deferred
            deferred_fields = instance.get_deferred_fields()
            if self.monitor in deferred_fields:
                return

            original = sender._default_manager.get(pk=instance.pk)
            original_value = getattr(original, self.monitor)
        except sender.DoesNotExist:
            original_value = None

        if current_value != original_value:
            # Value changed - update if when condition is met
            if self.when is None or current_value in self.when:
                setattr(instance, self.attname, timezone.now())

    def deconstruct(self) -> tuple[str, str, list[Any], dict[str, Any]]:
        name, path, args, kwargs = super().deconstruct()
        kwargs['monitor'] = self.monitor
        if self.when is not None:
            kwargs['when'] = self.when
        # Remove default if it's timezone.now
        if 'default' in kwargs and kwargs['default'] == timezone.now:
            del kwargs['default']
        return name, path, args, kwargs


class StatusField(models.CharField):
    """A CharField that auto-populates with STATUS choices."""

    def __init__(
        self,
        choices_name: str = 'STATUS',
        no_check_for_status: bool = False,
        **kwargs: Any,
    ) -> None:
        self.choices_name = choices_name
        self.no_check_for_status = no_check_for_status
        kwargs.setdefault('max_length', 100)
        super().__init__(**kwargs)

    def prepare_class(self, sender: type) -> None:
        if self.no_check_for_status:
            return
        if not hasattr(sender, self.choices_name):
            # During migrations, fake models may not have STATUS
            # Just silently return in that case
            return
        status_choices = getattr(sender, self.choices_name)
        self.choices = list(status_choices)
        if self.default is NOT_PROVIDED:
            self.default = self.choices[0][0]

    def contribute_to_class(self, cls: type, name: str, private_only: bool = False, **kwargs: Any) -> None:
        super().contribute_to_class(cls, name, private_only=private_only, **kwargs)
        models.signals.class_prepared.connect(self._prepare_class, sender=cls)

    def _prepare_class(self, sender: type, **kwargs: Any) -> None:
        self.prepare_class(sender)

    def deconstruct(self) -> tuple[str, str, list[Any], dict[str, Any]]:
        name, path, args, kwargs = super().deconstruct()
        if self.choices_name != 'STATUS':
            kwargs['choices_name'] = self.choices_name
        if self.no_check_for_status:
            kwargs['no_check_for_status'] = self.no_check_for_status
        # Don't include the dynamically set choices
        kwargs.pop('choices', None)
        return name, path, args, kwargs


class UUIDField(models.UUIDField):
    """A UUIDField that auto-generates UUIDs with configurable version."""

    def __init__(self, version: int = 4, **kwargs: Any) -> None:
        from django.core.exceptions import ValidationError

        if version not in (1, 3, 4, 5):
            raise ValidationError(f'UUID version {version} is not supported')

        self.version = version

        uuid_generators = {
            1: uuid.uuid1,
            3: uuid.uuid3,
            4: uuid.uuid4,
            5: uuid.uuid5,
        }
        kwargs.setdefault('default', uuid_generators[version])
        kwargs.setdefault('editable', False)
        super().__init__(**kwargs)

    def deconstruct(self) -> tuple[str, str, list[Any], dict[str, Any]]:
        name, path, args, kwargs = super().deconstruct()
        if self.version != 4:
            kwargs['version'] = self.version
        return name, path, args, kwargs


class UrlsafeTokenField(models.CharField):
    """A CharField that auto-generates URL-safe tokens."""

    _factory: Callable[[int], str] | None

    def __init__(
        self,
        editable: bool = False,
        max_length: int = 128,
        factory: Callable[[int], str] | None = None,
        **kwargs: Any,
    ) -> None:
        # Ignore default parameter - we always use our own
        kwargs.pop('default', None)
        if factory is not None and not callable(factory):
            raise TypeError("factory must be callable")
        self._factory = factory
        super().__init__(editable=editable, max_length=max_length, **kwargs)

    def get_default(self) -> str:
        if self._factory is not None:
            return self._factory(self.max_length)
        return secrets.token_urlsafe(self.max_length)[:self.max_length]

    def deconstruct(self) -> tuple[str, str, list[Any], dict[str, Any]]:
        name, path, args, kwargs = super().deconstruct()
        if self._factory is not None:
            kwargs['factory'] = self._factory
        return name, path, args, kwargs
