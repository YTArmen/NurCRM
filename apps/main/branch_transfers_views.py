from rest_framework import generics, permissions, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound
from django.db.models import Q
from django.shortcuts import get_object_or_404
from uuid import UUID

from apps.main.models import BranchTransfer
from apps.main.branch_transfers_serializers import (
    BranchTransferListSerializer,
    BranchTransferDetailSerializer,
)
from apps.main.services.branch_transfers import (
    create_and_execute_branch_transfer,
    cancel_branch_transfer,
    is_user_admin_or_owner,
    get_user_allowed_branch_ids,
)


class BranchTransferListCreateAPIView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]

    def _company(self):
        u = self.request.user
        return getattr(u, "owned_company", None) or getattr(u, "company", None)

    def get_serializer_class(self):
        if self.request.method == "POST":
            return BranchTransferDetailSerializer
        return BranchTransferListSerializer

    def get_queryset(self):
        company = self._company()
        if not company:
            return BranchTransfer.objects.none()

        user = self.request.user
        is_admin = is_user_admin_or_owner(user)
        if not is_admin and not getattr(user, "can_view_branch", False):
            raise PermissionDenied("У вас нет доступа к просмотру филиалов.")

        qs = (
            BranchTransfer.objects.filter(company=company)
            .select_related("from_branch", "to_branch", "created_by", "company")
            .prefetch_related("items")
            .order_by("-date", "-created_at")
        )

        # Если сотрудник ограничен филиалами, видит только свои
        if not is_admin:
            allowed_branches = get_user_allowed_branch_ids(user)
            if allowed_branches:
                qs = qs.filter(
                    Q(from_branch_id__in=allowed_branches) | Q(to_branch_id__in=allowed_branches)
                )

        qp = self.request.query_params

        # 1. Фильтр branch: отправитель ИЛИ получатель
        branch_param = (qp.get("branch") or "").strip()
        if branch_param:
            try:
                b_uuid = UUID(branch_param)
                qs = qs.filter(Q(from_branch_id=b_uuid) | Q(to_branch_id=b_uuid))
            except (ValueError, TypeError):
                qs = qs.none()

        # 2. Фильтр from_branch
        from_b = (qp.get("from_branch") or "").strip()
        if from_b:
            if from_b.lower() == "main":
                qs = qs.filter(from_branch__isnull=True)
            else:
                try:
                    fb_uuid = UUID(from_b)
                    qs = qs.filter(from_branch_id=fb_uuid)
                except (ValueError, TypeError):
                    qs = qs.none()

        # 3. Фильтр to_branch
        to_b = (qp.get("to_branch") or "").strip()
        if to_b:
            if to_b.lower() == "main":
                qs = qs.filter(to_branch__isnull=True)
            else:
                try:
                    tb_uuid = UUID(to_b)
                    qs = qs.filter(to_branch_id=tb_uuid)
                except (ValueError, TypeError):
                    qs = qs.none()

        # 4. Фильтр status
        st = (qp.get("status") or "").strip().lower()
        if st in (BranchTransfer.Status.COMPLETED, BranchTransfer.Status.CANCELLED):
            qs = qs.filter(status=st)

        # 5. Фильтры date_from / date_to
        df = (qp.get("date_from") or "").strip()
        if df:
            qs = qs.filter(date__gte=df)
        dt = (qp.get("date_to") or "").strip()
        if dt:
            qs = qs.filter(date__lte=dt)

        # 6. Поиск
        search = (qp.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(number__icontains=search)
                | Q(comment__icontains=search)
                | Q(from_branch__name__icontains=search)
                | Q(to_branch__name__icontains=search)
            )

        return qs

    def create(self, request, *args, **kwargs):
        idempotency_key = request.headers.get("Idempotency-Key") or request.META.get("HTTP_IDEMPOTENCY_KEY")
        if not idempotency_key and isinstance(request.data, dict):
            idempotency_key = str(request.data.get("idempotency_key") or "").strip()[:128] or None  # ТЗ 01.11
        # ТЗ кассы 05.11 п.17: повтор с тем же ключом — тот же документ и replayed: true
        replayed = bool(idempotency_key) and BranchTransfer.objects.filter(
            company=self._company(), idempotency_key=str(idempotency_key).strip()
        ).exists()
        transfer = create_and_execute_branch_transfer(
            user=request.user,
            data=request.data,
            idempotency_key=idempotency_key,
        )
        serializer = BranchTransferDetailSerializer(transfer, context={"request": request})
        return Response({**serializer.data, "replayed": replayed},
                        status=status.HTTP_200_OK if replayed else status.HTTP_201_CREATED)


class BranchTransferDetailAPIView(generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = BranchTransferDetailSerializer

    def get_queryset(self):
        u = self.request.user
        company = getattr(u, "owned_company", None) or getattr(u, "company", None)
        if not company:
            return BranchTransfer.objects.none()

        is_admin = is_user_admin_or_owner(u)
        if not is_admin and not getattr(u, "can_view_branch", False):
            raise PermissionDenied("У вас нет доступа к просмотру филиалов.")

        qs = (
            BranchTransfer.objects.filter(company=company)
            .select_related("from_branch", "to_branch", "created_by", "company")
            .prefetch_related("items")
        )

        if not is_admin:
            allowed_branches = get_user_allowed_branch_ids(u)
            if allowed_branches:
                qs = qs.filter(
                    Q(from_branch_id__in=allowed_branches) | Q(to_branch_id__in=allowed_branches)
                )

        return qs


class BranchTransferCancelAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk, *args, **kwargs):
        reason = request.data.get("reason", "")
        transfer = cancel_branch_transfer(
            user=request.user,
            transfer_id=pk,
            reason=reason,
        )
        serializer = BranchTransferDetailSerializer(transfer, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)
