"""ТЗ Н-33: таблицы размеров витрины — showcase/size-charts/ (CRUD владельца)."""
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status
from rest_framework.response import Response

from apps.main.models import SizeChart
from apps.main.showcase.views_design import EditorAPIView


class SizeChartSerializer(serializers.ModelSerializer):
    columns = serializers.ListField(child=serializers.CharField(max_length=64), max_length=20, required=False)
    rows = serializers.ListField(child=serializers.ListField(child=serializers.CharField(max_length=64, allow_blank=True), max_length=20),
                                 max_length=200, required=False)

    class Meta:
        model = SizeChart
        fields = ("id", "name", "columns", "rows", "note", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")

    def validate(self, attrs):
        cols = attrs.get("columns", getattr(self.instance, "columns", []))
        rows = attrs.get("rows", getattr(self.instance, "rows", []))
        if any(len(r) != len(cols) for r in rows):
            raise serializers.ValidationError({"rows": "Длина каждой строки должна совпадать с числом колонок."})
        return attrs


def public_size_chart(product):
    """Таблица размеров для публичной карточки (или None)."""
    chart = getattr(product, "size_chart", None)
    if chart is None:
        return None
    return {"id": str(chart.id), "name": chart.name, "columns": chart.columns, "rows": chart.rows, "note": chart.note}


class SizeChartListCreateAPIView(EditorAPIView):
    def get(self, request):
        qs = SizeChart.objects.filter(company=self.company())
        return Response(SizeChartSerializer(qs, many=True).data)

    def post(self, request):
        ser = SizeChartSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ser.save(company=self.company())
        return Response(ser.data, status=status.HTTP_201_CREATED)


class SizeChartDetailAPIView(EditorAPIView):
    def _obj(self, pk):
        return get_object_or_404(SizeChart, pk=pk, company=self.company())

    def get(self, request, pk):
        return Response(SizeChartSerializer(self._obj(pk)).data)

    def patch(self, request, pk):
        ser = SizeChartSerializer(self._obj(pk), data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)

    put = patch

    def delete(self, request, pk):
        self._obj(pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
