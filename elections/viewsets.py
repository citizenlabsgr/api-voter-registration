from rest_framework.viewsets import ModelViewSet

from . import models, serializers


class RegionTypeViewSet(ModelViewSet):
    http_method_names = ["get"]
    queryset = models.RegionType.objects.all()
    serializer_class = serializers.RegionTypeSerializer
