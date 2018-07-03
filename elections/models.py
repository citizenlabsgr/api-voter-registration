from typing import List

from django.db import models

import arrow
import log
from model_utils.models import TimeStampedModel

from . import helpers


class DistrictCategory(TimeStampedModel):
    """Types of regions bound to ballot items."""

    name = models.CharField(max_length=50, unique=True)

    class Meta:
        verbose_name_plural = "District Categories"

    def __str__(self) -> str:
        return self.name


class District(TimeStampedModel):
    """Districts bound to ballot items."""

    category = models.ForeignKey(DistrictCategory, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)

    class Meta:
        unique_together = ["category", "name"]

    def __str__(self) -> str:
        return self.name


class RegistrationStatus(models.Model):
    """Status of a particular voter's registration."""

    registered = models.BooleanField()
    districts: List[District] = []

    def save(self, *args, **kwargs):  # pylint: disable=arguments-differ
        raise NotImplementedError


class Voter(models.Model):
    """Data needed to look up Michigan voter registration status."""

    first_name = models.CharField(max_length=50)
    last_name = models.CharField(max_length=50)
    birth_date = models.DateField()
    zip_code = models.CharField(max_length=10)

    def __repr__(self) -> str:
        birth = arrow.get(self.birth_date).format("YYYY-MM-DD")
        return f"<voter: {self}, birth={birth}, zip={self.zip_code}>"

    def __str__(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def birth_month(self) -> str:
        locale = arrow.locales.get_locale("en")
        return locale.month_name(self.birth_date.month)

    @property
    def birth_year(self) -> int:
        return self.birth_date.year

    def fetch_registration_status(self) -> RegistrationStatus:
        data = helpers.fetch_registration_status_data(self)

        districts: List[District] = []
        for category_name, district_name in sorted(data["districts"].items()):
            if not (category_name and district_name):
                continue
            category, created = DistrictCategory.objects.get_or_create(
                name=category_name
            )
            if created:
                log.info(f"New category: {category}")
            district, created = District.objects.get_or_create(
                category=category, name=district_name
            )
            if created:
                log.info(f"New district: {district}")
            districts.append(district)

        status = RegistrationStatus(registered=data["registered"])
        status.districts = districts

        return status

    def save(self, *args, **kwargs):  # pylint: disable=arguments-differ
        raise NotImplementedError


class Election(TimeStampedModel):
    """Point in time where voters can cast opinions on ballot items."""

    date = models.DateField()
    name = models.CharField(max_length=100)
    reference_url = models.URLField(blank=True, null=True)

    class Meta:
        unique_together = ["date", "name"]


class Ballot(TimeStampedModel):
    """Full ballot bound to a particular precinct."""

    election = models.ForeignKey(Election, on_delete=models.CASCADE)

    county = models.ForeignKey(
        District, related_name="counties", on_delete=models.CASCADE
    )
    jurisdiction = models.ForeignKey(
        District, related_name="jurisdictions", on_delete=models.CASCADE
    )
    ward = models.PositiveIntegerField()
    precinct = models.PositiveIntegerField()

    mi_sos_url = models.URLField()


class BallotItem(TimeStampedModel):

    election = models.ForeignKey(Election, on_delete=models.CASCADE)

    district = models.ForeignKey(District, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    reference_url = models.URLField(blank=True, null=True)

    class Meta:
        abstract = True
        unique_together = ["election", "district", "name"]


class Proposal(BallotItem):
    """Ballot item with a boolean outcome."""


class Party(TimeStampedModel):
    """Affiliation for a particular candidate."""

    name = models.CharField(max_length=50)


class Candidate(TimeStampedModel):
    """Individual running for a particular position."""

    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    reference_url = models.URLField(blank=True, null=True)
    party = models.ForeignKey(
        Party, blank=True, null=True, on_delete=models.SET_NULL
    )


class Position(BallotItem):
    """Ballot item choosing one ore more candidates."""

    candidates = models.ManyToManyField(Candidate)
    seats = models.PositiveIntegerField(default=1)
