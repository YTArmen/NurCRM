import uuid
from zoneinfo import ZoneInfo
from django.db.models import Q, Prefetch
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.text import slugify

from rest_framework import generics, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .audit import create_platform_admin_audit_log
from .models import Company, Sector, SubscriptionPlan, Roles, CustomRole, PlatformAdminAuditLog, User, BranchMembership
from .permissions import IsPlatformAdmin
from .platform_admin_serializers import (
    PlatformAdminCompanyListSerializer,
    PlatformAdminCompanyDetailSerializer,
    PlatformAdminCompanyUpdateSerializer,
    PlatformAdminCompanySubscriptionUpdateSerializer,
    PlatformAdminUserSerializer,
    PlatformAdminUserCreateSerializer,
    PlatformAdminUserUpdateSerializer,
    generate_user_password,
)


class PlatformAdminPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class PlatformAdminMetaAPIView(APIView):
    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        sectors = [
            {
                "id": str(sector.id),
                "name": sector.name,
                "slug": slugify(sector.name, allow_unicode=True) or str(sector.id),
            }
            for sector in Sector.objects.order_by("name")
        ]
        plans = [
            {
                "id": str(plan.id),
                "name": plan.name,
            }
            for plan in SubscriptionPlan.objects.order_by("name")
        ]
        roles = [
            {
                "id": code,
                "name": label,
                "code": code,
                "company_id": None,
            }
            for code, label in Roles.choices
        ]
        roles.extend(
            {
                "id": str(role.id),
                "name": role.name,
                "code": None,
                "company_id": None,
            }
            for role in CustomRole.objects.filter(company__isnull=True).order_by("name")
        )

        return Response({"sectors": sectors, "plans": plans, "roles": roles})


def _is_valid_uuid(val):
    try:
        uuid.UUID(str(val))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


class PlatformAdminCompanyListAPIView(generics.ListAPIView):
    permission_classes = [IsPlatformAdmin]
    serializer_class = PlatformAdminCompanyListSerializer
    pagination_class = PlatformAdminPagination

    def get_queryset(self):
        qs = Company.objects.select_related("sector", "subscription_plan", "owner")

        # 1. Search
        search = (self.request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(slug__icontains=search)
                | Q(inn__icontains=search)
                | Q(owner__email__icontains=search)
            )

        # 2. Sector filter
        sector = (self.request.query_params.get("sector") or "").strip()
        if sector:
            if _is_valid_uuid(sector):
                qs = qs.filter(sector_id=sector)
            else:
                qs = qs.filter(sector__name__icontains=sector)

        # 3. Plan filter
        plan = (self.request.query_params.get("plan") or "").strip()
        if plan:
            if _is_valid_uuid(plan):
                qs = qs.filter(subscription_plan_id=plan)
            else:
                qs = qs.filter(subscription_plan__name__icontains=plan)

        # 4. Status filter (Asia/Bishkek calendar date)
        status_param = (self.request.query_params.get("status") or "").strip().lower()
        if status_param:
            bishkek_tz = ZoneInfo("Asia/Bishkek")
            now_bishkek = timezone.now().astimezone(bishkek_tz)
            today_start = timezone.datetime(
                now_bishkek.year,
                now_bishkek.month,
                now_bishkek.day,
                tzinfo=bishkek_tz,
            )

            if status_param == "blocked":
                qs = qs.filter(is_active=False)
            elif status_param == "expired":
                qs = qs.filter(is_active=True, end_date__lt=today_start)
            elif status_param == "missing_date":
                qs = qs.filter(is_active=True, end_date__isnull=True)
            elif status_param == "active":
                qs = qs.filter(is_active=True, end_date__gte=today_start)

        # 5. Ordering
        ordering = (self.request.query_params.get("ordering") or "").strip()
        allowed_ordering = {
            "name", "-name",
            "created_at", "-created_at",
            "end_date", "-end_date",
        }
        if ordering in allowed_ordering:
            qs = qs.order_by(ordering)
        else:
            qs = qs.order_by("-created_at")

        return qs


class PlatformAdminCompanyDetailAPIView(APIView):
    permission_classes = [IsPlatformAdmin]

    def _get_object(self, pk):
        qs = Company.objects.select_related(
            "sector", "subscription_plan", "owner"
        ).prefetch_related("branches", "custom_roles")
        return get_object_or_404(qs, pk=pk)

    def get(self, request, pk):
        company = self._get_object(pk)
        serializer = PlatformAdminCompanyDetailSerializer(company)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def patch(self, request, pk):
        company = self._get_object(pk)
        serializer = PlatformAdminCompanyUpdateSerializer(company, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        old_values = {}
        for field in serializer.validated_data.keys():
            if hasattr(company, field):
                old_values[field] = getattr(company, field)

        updated_company = serializer.save()

        # Build audit diff
        diff = {}
        for field, old_val in old_values.items():
            new_val = getattr(updated_company, field)
            if old_val != new_val:
                diff[field] = {
                    "old": str(old_val) if old_val is not None else None,
                    "new": str(new_val) if new_val is not None else None,
                }

        create_platform_admin_audit_log(
            actor=request.user,
            action=PlatformAdminAuditLog.Action.COMPANY_PATCH,
            object_type="company",
            object_id=str(updated_company.id),
            company_id=str(updated_company.id),
            payload=diff,
            request=request,
        )

        detail_serializer = PlatformAdminCompanyDetailSerializer(updated_company)
        return Response(detail_serializer.data, status=status.HTTP_200_OK)


class PlatformAdminCompanySubscriptionAPIView(APIView):
    permission_classes = [IsPlatformAdmin]

    def _get_object(self, pk):
        qs = Company.objects.select_related(
            "sector", "subscription_plan", "owner"
        ).prefetch_related("branches", "custom_roles")
        return get_object_or_404(qs, pk=pk)

    def patch(self, request, pk):
        company = self._get_object(pk)
        serializer = PlatformAdminCompanySubscriptionUpdateSerializer(
            company, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)

        old_values = {
            "subscription_plan_id": str(company.subscription_plan_id) if company.subscription_plan_id else None,
            "end_date": company.end_date.strftime("%Y-%m-%d") if company.end_date else None,
            "support_note": company.support_note,
        }

        updated_company = serializer.save()

        new_values = {
            "subscription_plan_id": str(updated_company.subscription_plan_id) if updated_company.subscription_plan_id else None,
            "end_date": updated_company.end_date.strftime("%Y-%m-%d") if updated_company.end_date else None,
            "support_note": updated_company.support_note,
        }

        diff = {}
        for key, old_val in old_values.items():
            new_val = new_values.get(key)
            if old_val != new_val:
                diff[key] = {
                    "old": old_val,
                    "new": new_val,
                }

        create_platform_admin_audit_log(
            actor=request.user,
            action=PlatformAdminAuditLog.Action.COMPANY_SUBSCRIPTION,
            object_type="company",
            object_id=str(updated_company.id),
            company_id=str(updated_company.id),
            payload=diff,
            request=request,
        )

        detail_serializer = PlatformAdminCompanyDetailSerializer(updated_company)
        return Response(detail_serializer.data, status=status.HTTP_200_OK)


class PlatformAdminCompanyUserListCreateAPIView(APIView):
    permission_classes = [IsPlatformAdmin]
    pagination_class = PlatformAdminPagination

    def _get_company(self, company_id):
        return get_object_or_404(Company, pk=company_id)

    def get(self, request, company_id):
        company = self._get_company(company_id)
        qs = (
            User.objects.filter(company=company, deleted_at__isnull=True)
            .select_related("company", "custom_role")
            .prefetch_related("branch_memberships")
            .order_by("-created_at")
        )

        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(email__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
            )

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(qs, request, view=self)
        if page is not None:
            serializer = PlatformAdminUserSerializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)

        serializer = PlatformAdminUserSerializer(qs, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request, company_id):
        company = self._get_company(company_id)
        serializer = PlatformAdminUserCreateSerializer(
            data=request.data,
            context={"company": company, "request": request},
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        # Audit log
        create_platform_admin_audit_log(
            actor=request.user,
            action=PlatformAdminAuditLog.Action.USER_CREATE,
            object_type="user",
            object_id=str(user.id),
            company_id=str(company.id),
            payload={
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "role": user.role,
                "custom_role_id": str(user.custom_role_id) if user.custom_role_id else None,
            },
            request=request,
        )

        response_data = PlatformAdminUserSerializer(user).data
        response_data["generated_password"] = getattr(user, "generated_password", None)
        return Response(response_data, status=status.HTTP_201_CREATED)


class PlatformAdminUserDetailAPIView(APIView):
    permission_classes = [IsPlatformAdmin]

    def _get_object(self, pk):
        return get_object_or_404(
            User.objects.filter(deleted_at__isnull=True)
            .select_related("company", "custom_role")
            .prefetch_related("branch_memberships"),
            pk=pk,
        )

    def get(self, request, pk):
        user = self._get_object(pk)
        serializer = PlatformAdminUserSerializer(user)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def patch(self, request, pk):
        user = self._get_object(pk)
        old_values = {}
        fields_to_track = [
            "email", "first_name", "last_name", "phone_number", "avatar",
            "track_number", "role", "custom_role_id", "is_active",
        ] + [f.name for f in User._meta.fields if f.name.startswith("can_view_") or f.name.startswith("can_manage_")]
        for f in fields_to_track:
            old_values[f] = getattr(user, f, None)

        serializer = PlatformAdminUserUpdateSerializer(
            user,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        updated_user = serializer.save()

        # Build audit diff
        diff = {}
        for f in fields_to_track:
            new_val = getattr(updated_user, f, None)
            old_val = old_values.get(f)
            if old_val != new_val:
                diff[f] = {
                    "old": str(old_val) if old_val is not None else None,
                    "new": str(new_val) if new_val is not None else None,
                }

        create_platform_admin_audit_log(
            actor=request.user,
            action=PlatformAdminAuditLog.Action.USER_PATCH,
            object_type="user",
            object_id=str(updated_user.id),
            company_id=str(updated_user.company_id) if updated_user.company_id else None,
            payload=diff,
            request=request,
        )

        detail_serializer = PlatformAdminUserSerializer(updated_user)
        return Response(detail_serializer.data, status=status.HTTP_200_OK)

    def delete(self, request, pk):
        user = self._get_object(pk)
        email = user.email
        company_id = str(user.company_id) if user.company_id else None

        user.soft_delete(by_user=request.user)

        create_platform_admin_audit_log(
            actor=request.user,
            action=PlatformAdminAuditLog.Action.USER_DELETE,
            object_type="user",
            object_id=str(user.id),
            company_id=company_id,
            payload={"email": email},
            request=request,
        )

        return Response(status=status.HTTP_204_NO_CONTENT)


class PlatformAdminUserResetPasswordAPIView(APIView):
    permission_classes = [IsPlatformAdmin]

    def post(self, request, pk):
        user = get_object_or_404(User.objects.filter(deleted_at__isnull=True), pk=pk)
        new_password = generate_user_password(10)
        user.set_password(new_password)
        user.save(update_fields=["password"])

        create_platform_admin_audit_log(
            actor=request.user,
            action=PlatformAdminAuditLog.Action.USER_RESET_PASSWORD,
            object_type="user",
            object_id=str(user.id),
            company_id=str(user.company_id) if user.company_id else None,
            payload={"target_user_id": str(user.id), "email": user.email},
            request=request,
        )

        return Response({"generated_password": new_password}, status=status.HTTP_200_OK)


class PlatformAdminUserImpersonateAPIView(APIView):
    permission_classes = [IsPlatformAdmin]

    def post(self, request, pk):
        target_user = (
            User.objects.filter(deleted_at__isnull=True, pk=pk)
            .select_related("company", "custom_role")
            .prefetch_related(
                Prefetch(
                    "branch_memberships",
                    queryset=BranchMembership.objects.select_related("branch"),
                ),
            )
            .first()
        )
        if not target_user:
            return Response({"detail": "Пользователь не найден."}, status=status.HTTP_404_NOT_FOUND)

        if getattr(target_user, "is_platform_admin", False) or getattr(target_user, "is_superuser", False):
            return Response(
                {"detail": "Нельзя войти от имени платформенного администратора."},
                status=status.HTTP_403_FORBIDDEN,
            )

        refresh = RefreshToken.for_user(target_user)
        refresh["impersonated_by"] = str(request.user.id)
        from apps.users.auth_session import stamp_auth_time

        stamp_auth_time(refresh)
        access = refresh.access_token
        access["impersonated_by"] = str(request.user.id)

        branch_ids = []
        primary_branch_id = None
        for mb in target_user.branch_memberships.all():
            branch_ids.append(mb.branch_id)
            if mb.is_primary:
                primary_branch_id = mb.branch_id

        data = {
            "access": str(access),
            "refresh": str(refresh),
            "accessToken": str(access),
            "refreshToken": str(refresh),
            "user_id": target_user.id,
            "email": target_user.email,
            "first_name": target_user.first_name,
            "last_name": target_user.last_name,
            "avatar": target_user.avatar,
            "phone_number": target_user.phone_number,
            "track_number": target_user.track_number,
            "company": target_user.company.name if target_user.company else None,
            "role": target_user.role_display,
            "is_platform_admin": False,
            "branch_ids": branch_ids,
            "primary_branch_id": primary_branch_id,
        }

        create_platform_admin_audit_log(
            actor=request.user,
            action=PlatformAdminAuditLog.Action.USER_IMPERSONATE,
            object_type="user",
            object_id=str(target_user.id),
            company_id=str(target_user.company_id) if target_user.company_id else None,
            payload={"target_user_id": str(target_user.id), "email": target_user.email},
            request=request,
        )

        return Response(data, status=status.HTTP_200_OK)
