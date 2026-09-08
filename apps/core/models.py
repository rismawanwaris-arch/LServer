from django.db import models

MONEY = {"max_digits": 15, "decimal_places": 2}


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


def money_field(**kwargs):
    return models.DecimalField(**MONEY, **kwargs)
