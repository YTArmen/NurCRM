from rest_framework import status, permissions, filters
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView
from apps.users.permissions import read_only_guard
from rest_framework import generics
from django.shortcuts import get_object_or_404
from django.db.models import Q, Prefetch
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend

import logging
from datetime import timedelta
from decimal import Decimal

from . import models, serializers_documents, services, services_money
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.core.exceptions import ValidationError as DjangoValidationError
from .views import (
    CompanyBranchRestrictedMixin,
    filter_qs_company_branch_or_global,
    _parse_scale_barcode,
    ProtectedProductDeleteMixin,
    ProductCatalogPagination,
)
from .filters import ProductFilter
from .validators import WarehouseGuardMixin, ensure_unique_counterparty
from apps.utils import _is_owner_like

logger = logging.getLogger(__name__)


def _truthy(raw) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in ("true", "1", "yes", "y", "on")


def _service_error_response(exc):
    return Response(services.error_payload(exc), status=services.error_status(exc))


def _raise_service_error(exc):
    from rest_framework.exceptions import PermissionDenied

    if services.error_status(exc) == 403:
        raise PermissionDenied(services.error_payload(exc))
    raise DRFValidationError(services.error_payload(exc))


def _agent_allowed_for_company(agent_user, company):
    """Агент допустим для компании: сотрудник (company_id) или активный агент (CompanyWarehouseAgent)."""
    if not agent_user or not company:
        return False
    if getattr(agent_user, "company_id", None) == getattr(company, "id", None):
        return True
    return models.CompanyWarehouseAgent.objects.filter(
        user=agent_user,
        company=company,
        status=models.CompanyWarehouseAgent.Status.ACTIVE,
    ).exists()


@read_only_guard("can_edit_documents")
class DocumentListCreateView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = serializers_documents.DocumentSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["doc_type", "status", "payment_kind", "warehouse_from", "warehouse_to", "counterparty"]
    search_fields = [
        "number",
        "comment",
        "counterparty__name",
        "counterparty__phone",
        "agent__first_name",
        "agent__last_name",
        "agent__email",
        "agent__phone_number",
    ]

    def filter_queryset(self, queryset):
        if "search" in self.request.query_params:
            raw_search = self.request.query_params.get("search", "")
            cleaned_search = raw_search.strip()
            if cleaned_search != raw_search:
                q = self.request.query_params.copy()
                q["search"] = cleaned_search
                self.request._request.GET = q
        return super().filter_queryset(queryset)

    
    def _filter_company_branch(self, qs):
        company = self._company()
        if company is None:
            return qs.none()

        qs = qs.filter(
            Q(warehouse_from__company=company) | Q(warehouse_to__company=company)
        )

        branch = self._auto_branch()
        if branch is not None:
            qs = qs.filter(
                Q(warehouse_from__branch=branch) | Q(warehouse_to__branch=branch)
            )
        assigned_warehouse_id = self._assigned_agent_warehouse_id(company=company)
        if assigned_warehouse_id and not _is_owner_like(self.request.user):
            qs = qs.filter(warehouse_from_id=assigned_warehouse_id)
        return qs

    def get_queryset(self):
        # Оптимизация: предзагружаем связанные объекты
        qs = models.Document.objects.select_related(
            "warehouse_from", "warehouse_to", "counterparty", "agent"
        ).prefetch_related(
            "items__product",
            "items__product__warehouse",
            Prefetch(
                "items__product__images",
                queryset=models.WarehouseProductImage.objects.order_by("-is_primary", "created_at"),
            ),
            "moves__warehouse",
            "moves__product",
        ).order_by("-date")
        qs = self._filter_company_branch(qs)
        # Необязательная фильтрация по операционной дате документа:
        # ?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD (по полю date, не created_at).
        qs = services_money.apply_requested_date_range(qs, "date", self)
        qs = self._apply_extra_filters(qs)
        user = self.request.user
        if not _is_owner_like(user):
            qs = qs.filter(agent=user)
        return qs

    def _apply_extra_filters(self, qs):
        """
        ?posted_negative=true — проведённые в минус (B02);
        ?stale_days=N — «зависшие»: ждут кассу / заявка дольше N дней (B43);
        ?base_document=<uuid> — возвраты по документу (B04).
        """
        params = self.request.query_params
        if "posted_negative" in params:
            if _truthy(params.get("posted_negative")):
                qs = qs.filter(posted_negative_at__isnull=False)
            else:
                qs = qs.filter(posted_negative_at__isnull=True)
        raw_stale = (params.get("stale_days") or "").strip()
        if raw_stale:
            try:
                days = max(0, int(raw_stale))
            except ValueError:
                raise DRFValidationError({"stale_days": "Укажите целое число дней."})
            border = timezone.now() - timedelta(days=days)
            qs = qs.filter(
                Q(status=models.Document.Status.CASH_PENDING)
                & (Q(cash_request__requested_at__lte=border) | Q(cash_request__isnull=True, updated_at__lte=border))
                | Q(status=models.Document.Status.SALE_REQUEST, updated_at__lte=border)
            )
        raw_base = (params.get("base_document") or "").strip()
        if raw_base:
            qs = qs.filter(base_document_id=raw_base)
        return qs

    def _enforce_wholesale_permission(self, serializer, user):
        """Агенту (не владельцу) опт доступен только если владелец выдал флаг can_sell_wholesale."""
        if _is_owner_like(user):
            return
        if not serializer.validated_data.get("is_wholesale"):
            return
        wh_from = serializer.validated_data.get("warehouse_from")
        company = getattr(wh_from, "company", None)
        if not services.agent_can_sell_wholesale(user=user, company=company):
            raise DRFValidationError(
                {"is_wholesale": "У агента нет доступа к оптовым продажам. Обратитесь к владельцу."}
            )

    def perform_create(self, serializer):
        user = self.request.user
        if _is_owner_like(user):
            self._ensure_agent_can_access_warehouse(serializer.validated_data.get("warehouse_from"), field_name="warehouse_from")
            self._ensure_agent_can_access_warehouse(serializer.validated_data.get("warehouse_to"), field_name="warehouse_to")
            self._save_with_company_branch(serializer)
            return
        self._ensure_agent_can_access_warehouse(serializer.validated_data.get("warehouse_from"), field_name="warehouse_from")
        self._ensure_agent_can_access_warehouse(serializer.validated_data.get("warehouse_to"), field_name="warehouse_to")
        self._enforce_wholesale_permission(serializer, user)
        self._save_with_company_branch(serializer, agent=user)


class AgentDocumentListCreateView(DocumentListCreateView):
    """
    Документы агента (операции по своим товарам).
    """
    def get_queryset(self):
        qs = super().get_queryset()
        return qs.filter(agent=self.request.user)

    def perform_create(self, serializer):
        user = self.request.user
        wh_from = serializer.validated_data.get("warehouse_from")
        self._ensure_agent_can_access_warehouse(wh_from, field_name="warehouse_from")

        self._enforce_wholesale_permission(serializer, user)

        use_common_stock = bool(serializer.validated_data.get("use_common_stock", False))
        if not use_common_stock and wh_from is not None:
            if services.agent_has_common_access_to_warehouse(
                user=user,
                warehouse=wh_from,
                company=getattr(wh_from, "company", None),
            ):
                use_common_stock = True

        serializer.save(agent=user, use_common_stock=use_common_stock)


class _DocumentTypedListCreateView(DocumentListCreateView):
    """
    Базовый класс для списков по одному типу документа.
    """
    DOC_TYPE = None  # override in subclasses

    def get_queryset(self):
        qs = super().get_queryset()
        if self.DOC_TYPE:
            qs = qs.filter(doc_type=self.DOC_TYPE)
        return qs

    def perform_create(self, serializer):
        extra = {}
        if self.DOC_TYPE:
            extra["doc_type"] = self.DOC_TYPE
        user = self.request.user
        self._ensure_agent_can_access_warehouse(serializer.validated_data.get("warehouse_from"), field_name="warehouse_from")
        self._ensure_agent_can_access_warehouse(serializer.validated_data.get("warehouse_to"), field_name="warehouse_to")
        if _is_owner_like(user):
            validated_agent = serializer.validated_data.get("agent")
            if getattr(validated_agent, "id", None) == getattr(user, "id", None):
                # Owner sale must use warehouse stock even if the client sends agent=self.
                extra["agent"] = None
            self._save_with_company_branch(serializer, **extra)
            return
        self._enforce_wholesale_permission(serializer, user)
        wh_from = serializer.validated_data.get("warehouse_from")
        use_common_stock = False
        if wh_from is not None and services.agent_has_common_access_to_warehouse(
            user=user,
            warehouse=wh_from,
            company=getattr(wh_from, "company", None),
        ):
            use_common_stock = True
        self._save_with_company_branch(serializer, agent=user, use_common_stock=use_common_stock, **extra)


class DocumentSaleListCreateView(_DocumentTypedListCreateView):
    DOC_TYPE = models.Document.DocType.SALE


class DocumentPurchaseListCreateView(_DocumentTypedListCreateView):
    DOC_TYPE = models.Document.DocType.PURCHASE


class DocumentSaleReturnListCreateView(_DocumentTypedListCreateView):
    DOC_TYPE = models.Document.DocType.SALE_RETURN


class DocumentPurchaseReturnListCreateView(_DocumentTypedListCreateView):
    DOC_TYPE = models.Document.DocType.PURCHASE_RETURN


class DocumentInventoryListCreateView(_DocumentTypedListCreateView):
    DOC_TYPE = models.Document.DocType.INVENTORY


class DocumentReceiptListCreateView(_DocumentTypedListCreateView):
    DOC_TYPE = models.Document.DocType.RECEIPT


class DocumentWriteOffListCreateView(_DocumentTypedListCreateView):
    DOC_TYPE = models.Document.DocType.WRITE_OFF


class DocumentCommercialOfferListCreateView(_DocumentTypedListCreateView):
    """
    Коммерческие предложения (без проведения/остатков).
    """
    DOC_TYPE = models.Document.DocType.COMMERCIAL_OFFER


class DocumentTransferListCreateView(_DocumentTypedListCreateView):
    """
    Документы перемещения. При создании автоматически проводится —
    остатки снимаются со склада-источника и добавляются на склад-приёмник.
    """
    DOC_TYPE = models.Document.DocType.TRANSFER

    def perform_create(self, serializer):
        super().perform_create(serializer)
        doc = serializer.instance
        if doc and doc.status == doc.Status.DRAFT:
            try:
                allow_negative = _truthy(self.request.data.get("allow_negative", False))
                services.post_document(doc, allow_negative=allow_negative, user=self.request.user)
                doc.refresh_from_db()
            except Exception as e:
                _raise_service_error(e)


@read_only_guard("can_edit_documents")
class DocumentDetailView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = serializers_documents.DocumentSerializer
    
    def get_queryset(self):
        # Оптимизация: предзагружаем связанные объекты
        qs = models.Document.objects.select_related(
            "warehouse_from", "warehouse_to", "counterparty", "agent"
        ).prefetch_related(
            "items__product",
            "items__product__brand",
            "items__product__category",
            "items__product__warehouse",
            Prefetch(
                "items__product__images",
                queryset=models.WarehouseProductImage.objects.order_by("-is_primary", "created_at"),
            ),
            "moves__warehouse",
            "moves__product",
        )
        qs = DocumentListCreateView._filter_company_branch(self, qs)
        user = self.request.user
        if not _is_owner_like(user):
            qs = qs.filter(agent=user)
        return qs

    def perform_destroy(self, instance):
        """
        Удалить можно только черновик без движений и денег (documents-delete-draft.md).
        Проведённый документ сначала распроводят: иначе его движения пропали бы из
        истории, а остатки и касса остались бы изменёнными.
        """
        doc = instance
        if doc.status != models.Document.Status.DRAFT:
            raise DRFValidationError({"detail": "Удалить можно только черновик. Сначала отмените проведение."})
        try:
            services.ensure_period_open(
                company=services.document_company(doc), dates=[doc.date], user=self.request.user
            )
        except Exception as e:
            _raise_service_error(e)
        if doc.is_sale_request or models.AgentRequestCart.objects.filter(sale_document=doc).exists():
            raise DRFValidationError({"detail": "Заявку агента нельзя удалить как черновик."})
        if doc.moves.exists() or doc.agent_moves.exists():
            raise DRFValidationError({"detail": "У документа есть движения товара — удаление запрещено."})
        if models.MoneyDocument.objects.filter(source_document=doc).exists():
            raise DRFValidationError({"detail": "У документа есть денежный документ — удаление запрещено."})
        if models.CashApprovalRequest.objects.filter(document=doc).exclude(
            status=models.CashApprovalRequest.Status.REJECTED
        ).exists():
            raise DRFValidationError({"detail": "По документу есть запрос в кассу — удаление запрещено."})
        if doc.salary_accruals.exists():
            raise DRFValidationError({"detail": "По документу начислена зарплата — удаление запрещено."})
        logger.info(
            "warehouse document deleted: id=%s number=%s type=%s by user=%s",
            doc.pk, doc.number, doc.doc_type, getattr(self.request.user, "pk", None),
        )
        doc.delete()


class DocumentScanView(CompanyBranchRestrictedMixin, APIView):
    """
    Разрешение штрихкода в строку документа (продажа/возврат/приход и т.п.).

    POST /api/warehouse/documents/scan/
    Body:
        {
          "barcode": "4600...",          # обязательно
          "warehouse": "<uuid>",          # опционально — уточнить склад при совпадении кода
          "doc_type": "SALE",             # опционально (по умолчанию SALE) — влияет на цену
          "is_wholesale": false           # опционально — для SALE подставит оптовую цену
        }

    Возвращает готовую строку для добавления в `items` документа. Товар НЕ создаётся.
        200 — найден ровно один товар.
        404 — товар по штрихкоду не найден.
        409 — штрихкод есть на нескольких складах, нужно уточнить `warehouse`.
    """

    def _base_products_qs(self):
        qs = (
            models.WarehouseProduct.objects
            .select_related("warehouse", "company", "branch", "characteristics")
            .prefetch_related("images", "alternate_barcodes")
        )
        return self._filter_qs_company_branch(qs)

    def _resolve_warehouse(self, warehouse_id):
        if not warehouse_id:
            return None
        wh = self._filter_qs_company_branch(models.Warehouse.objects.all()).filter(id=warehouse_id).first()
        if wh is None:
            raise DRFValidationError({"warehouse": "Склад не найден или недоступен."})
        return wh

    def _suggested_price(self, product, doc_type, is_wholesale):
        retail = Decimal(str(product.price or 0))
        wholesale = Decimal(str(product.wholesale_price or 0))
        if doc_type == models.Document.DocType.SALE and is_wholesale and wholesale > 0:
            chosen = wholesale
        else:
            chosen = retail
        return chosen.quantize(Decimal("0.01"))

    def _product_image_url(self, request, product):
        img = next(iter(product.images.all()), None)
        if not img or not getattr(img, "image", None):
            img = (
                models.WarehouseProductImage.objects.filter(product=product)
                .order_by("-is_primary", "created_at")
                .first()
            )
        if not img or not getattr(img, "image", None):
            return None
        url = img.image.url
        return request.build_absolute_uri(url) if request else url

    def _line_payload(self, request, product, *, doc_type, is_wholesale, scan_qty, barcode):
        qty = scan_qty if scan_qty is not None else Decimal("1")
        return {
            "product": str(product.id),
            "product_name": product.name,
            "product_article": product.article or "",
            "warehouse": str(product.warehouse_id) if product.warehouse_id else None,
            "warehouse_name": getattr(product.warehouse, "name", None),
            "barcode": barcode,
            "unit": product.unit,
            "is_weight": bool(product.is_weight),
            "available_qty": str(product.quantity),
            "qty": str(qty.quantize(Decimal("0.001"))),
            "price": str(self._suggested_price(product, doc_type, is_wholesale)),
            "product_price": str(product.price),
            "product_wholesale_price": str(product.wholesale_price),
            "product_discount_percent": str(product.discount_percent),
            "product_image_url": self._product_image_url(request, product),
        }

    def post(self, request, *args, **kwargs):
        barcode = (request.data.get("barcode") or "").strip()
        if not barcode:
            raise DRFValidationError({"barcode": "Обязательное поле."})

        doc_type = (request.data.get("doc_type") or models.Document.DocType.SALE)
        is_wholesale = request.data.get("is_wholesale", False)
        if isinstance(is_wholesale, str):
            is_wholesale = is_wholesale.strip().lower() in ("true", "1", "yes")

        warehouse = self._resolve_warehouse(request.data.get("warehouse"))

        qs = self._base_products_qs()
        if warehouse is not None:
            qs = qs.filter(warehouse=warehouse)

        scan_qty = None
        matches = list(
            qs.filter(Q(barcode=barcode) | Q(alternate_barcodes__barcode=barcode)).distinct()
        )

        if not matches:
            scale_data = _parse_scale_barcode(barcode)
            if scale_data:
                scan_qty = models.q_qty(Decimal(scale_data["weight_kg"]))
                matches = list(qs.filter(plu=scale_data["plu"]))

        if not matches:
            return Response(
                {"detail": "Товар по штрихкоду не найден.", "barcode": barcode},
                status=status.HTTP_404_NOT_FOUND,
            )

        if len(matches) > 1:
            return Response(
                {
                    "detail": "Штрихкод найден на нескольких складах — уточните warehouse.",
                    "barcode": barcode,
                    "candidates": [
                        {
                            "product": str(p.id),
                            "warehouse": str(p.warehouse_id) if p.warehouse_id else None,
                            "warehouse_name": getattr(p.warehouse, "name", None),
                            "available_qty": str(p.quantity),
                        }
                        for p in matches
                    ],
                },
                status=status.HTTP_409_CONFLICT,
            )

        product = matches[0]
        return Response(
            self._line_payload(
                request,
                product,
                doc_type=doc_type,
                is_wholesale=is_wholesale,
                scan_qty=scan_qty,
                barcode=barcode,
            ),
            status=status.HTTP_200_OK,
        )


class AgentDocumentDetailView(DocumentDetailView):
    def get_queryset(self):
        qs = super().get_queryset()
        return qs.filter(agent=self.request.user)


@read_only_guard("can_edit_documents")
class DocumentPostView(CompanyBranchRestrictedMixin, generics.GenericAPIView):
    serializer_class = serializers_documents.DocumentSerializer
    
    def get_queryset(self):
        # Оптимизация: предзагружаем items с продуктами
        qs = models.Document.objects.select_related(
            "warehouse_from", "warehouse_to", "counterparty", "agent"
        ).prefetch_related("items__product", "items__product__warehouse")
        user = self.request.user
        qs = DocumentListCreateView._filter_company_branch(self, qs)
        if not _is_owner_like(user):
            qs = qs.filter(agent=user)
        return qs

    def post(self, request, pk=None):
        doc = self.get_object()

        # 1. Если документ уже проведен (POSTED), возвращаем 200 OK (идемпотентно)
        if doc.status == doc.Status.POSTED:
            return Response(self.get_serializer(doc).data, status=status.HTTP_200_OK)

        # 2. Если документ ожидает решения кассы (CASH_PENDING), утверждаем кассовый запрос до POSTED
        if doc.status == doc.Status.CASH_PENDING:
            try:
                services.approve_cash_request(
                    doc, decided_by=request.user,
                    allow_negative_cash=_truthy(request.data.get("allow_negative_cash")),
                )
            except Exception as e:
                return _service_error_response(e)
            doc.refresh_from_db()
            return Response(self.get_serializer(doc).data, status=status.HTTP_200_OK)

        # 3. Разрешены только черновики (DRAFT) и заявки на продажу (SALE_REQUEST)
        if doc.status not in (doc.Status.DRAFT, doc.Status.SALE_REQUEST):
            return Response(
                {"detail": "Провести можно только черновик, заявку на продажу или документ, ожидающий кассу."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            from decimal import Decimal, InvalidOperation
            from .utils import normalize_payment_kind

            update_fields = []
            if "payment_kind" in request.data:
                doc.payment_kind = normalize_payment_kind(request.data.get("payment_kind"))
                update_fields.append("payment_kind")
            if "prepayment_amount" in request.data:
                try:
                    doc.prepayment_amount = Decimal(str(request.data.get("prepayment_amount") or "0")).quantize(
                        Decimal("0.01")
                    )
                except (InvalidOperation, TypeError, ValueError):
                    return Response({"prepayment_amount": "Некорректная сумма предоплаты."}, status=status.HTTP_400_BAD_REQUEST)
                update_fields.append("prepayment_amount")
            if update_fields:
                try:
                    doc.clean()
                except DjangoValidationError as exc:
                    raise DRFValidationError(getattr(exc, "message_dict", {"detail": str(exc)}))
                doc.save(update_fields=update_fields)

            # Позволяем передать allow_negative в теле запроса для обхода проверки остатков
            allow_negative = request.data.get('allow_negative', False)
            if isinstance(allow_negative, str):
                allow_negative = allow_negative.lower() in ('true', '1', 'yes')
            # Осознанное повторение такого же документа (редкий, но законный случай):
            # ?allow_duplicate=true снимает защиту от дубля.
            allow_duplicate = request.data.get('allow_duplicate', False)
            if isinstance(allow_duplicate, str):
                allow_duplicate = allow_duplicate.lower() in ('true', '1', 'yes')
            services.post_document(
                doc,
                allow_negative=allow_negative,
                user=request.user,
                allow_duplicate=bool(allow_duplicate),
                allow_negative_cash=_truthy(request.data.get("allow_negative_cash")),
            )
            doc.refresh_from_db()

            # Если при проведении наличной продажи документ перешёл в CASH_PENDING (включено подтверждение кассы):
            # Если в запросе явно передан auto_approve_cash, учитываем его значение.
            raw_auto_approve = request.data.get("auto_approve_cash", None)
            if raw_auto_approve is not None:
                if isinstance(raw_auto_approve, str):
                    auto_approve_cash = raw_auto_approve.lower() in ("true", "1", "yes")
                else:
                    auto_approve_cash = bool(raw_auto_approve)
            else:
                auto_approve_cash = False

            if doc.status == doc.Status.CASH_PENDING and auto_approve_cash:
                try:
                    services.approve_cash_request(doc, decided_by=request.user)
                except Exception as e:
                    logger.warning("Auto approve cash request failed: %s", e)

        except Exception as e:
            return _service_error_response(e)
        doc.refresh_from_db()
        return Response(self.get_serializer(doc).data)


@read_only_guard("can_edit_documents")
class DocumentUnpostView(CompanyBranchRestrictedMixin, generics.GenericAPIView):
    serializer_class = serializers_documents.DocumentSerializer
    
    def get_queryset(self):
        # Оптимизация: предзагружаем moves с продуктами и складами
        qs = models.Document.objects.select_related(
            "warehouse_from", "warehouse_to", "counterparty", "agent"
        ).prefetch_related("moves__warehouse", "moves__product")
        qs = DocumentListCreateView._filter_company_branch(self, qs)
        user = self.request.user
        if not _is_owner_like(user):
            qs = qs.filter(agent=user)
        return qs

    def post(self, request, pk=None):
        doc = self.get_object()
        if (
            doc.doc_type == models.Document.DocType.TRANSFER
            and doc.warehouse_from_id and doc.warehouse_to_id
            and doc.warehouse_from.company_id != doc.warehouse_to.company_id
        ):
            # stock-partnership §7.11 (D7): межкомпанейское — только получатель (у него списывается товар)
            from apps.warehouse.views_partnership import user_can_manage_partnership

            if not user_can_manage_partnership(request.user, doc.warehouse_to.company):
                return Response(
                    {"detail": "Отменить межкомпанейское перемещение может только получатель."},
                    status=status.HTTP_403_FORBIDDEN,
                )
        try:
            services.unpost_document(
                doc,
                user=request.user,
                allow_negative=_truthy(request.data.get("allow_negative")),
            )
        except Exception as e:
            return _service_error_response(e)
        return Response(self.get_serializer(doc).data)


class DocumentCashApproveView(CompanyBranchRestrictedMixin, generics.GenericAPIView):
    serializer_class = serializers_documents.DocumentSerializer

    def get_queryset(self):
        qs = models.Document.objects.select_related(
            "warehouse_from", "warehouse_to", "counterparty", "cash_register", "payment_category", "agent"
        )
        qs = DocumentListCreateView._filter_company_branch(self, qs)
        user = self.request.user
        if not _is_owner_like(user):
            qs = qs.filter(agent=user)
        return qs

    def post(self, request, pk=None):
        doc = self.get_object()
        note = (request.data.get("note") or "").strip()
        try:
            services.approve_cash_request(
                doc, decided_by=request.user, note=note,
                allow_negative_cash=_truthy(request.data.get("allow_negative_cash")),
            )
        except Exception as e:
            return _service_error_response(e)
        doc.refresh_from_db()
        return Response(self.get_serializer(doc).data, status=status.HTTP_200_OK)


class DocumentCashRejectView(CompanyBranchRestrictedMixin, generics.GenericAPIView):
    serializer_class = serializers_documents.DocumentSerializer

    def get_queryset(self):
        qs = models.Document.objects.select_related(
            "warehouse_from", "warehouse_to", "counterparty", "cash_register", "payment_category", "agent"
        )
        qs = DocumentListCreateView._filter_company_branch(self, qs)
        user = self.request.user
        if not _is_owner_like(user):
            qs = qs.filter(agent=user)
        return qs

    def post(self, request, pk=None):
        doc = self.get_object()
        note = (request.data.get("note") or "").strip()
        try:
            services.reject_cash_request(doc, decided_by=request.user, note=note)
        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        doc.refresh_from_db()
        return Response(self.get_serializer(doc).data, status=status.HTTP_200_OK)


class DocumentReturnableView(CompanyBranchRestrictedMixin, generics.GenericAPIView):
    """
    QA B04: сколько можно вернуть по продаже/закупу.
    GET /api/warehouse/documents/{id}/returnable/
    → {"items": [{"base_item", "product", "name", "sold", "returned", "returnable", "price"}]}
    """

    serializer_class = serializers_documents.DocumentSerializer

    def get_queryset(self):
        qs = models.Document.objects.select_related("warehouse_from", "warehouse_to", "counterparty")
        qs = DocumentListCreateView._filter_company_branch(self, qs)
        if not _is_owner_like(self.request.user):
            qs = qs.filter(agent=self.request.user)
        return qs

    def get(self, request, pk=None):
        doc = self.get_object()
        if doc.doc_type not in (models.Document.DocType.SALE, models.Document.DocType.PURCHASE):
            return Response(
                {"detail": "Возврат оформляется только по продаже или закупу.", "code": "return_base_invalid"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({
            "document": str(doc.pk),
            "number": doc.number,
            "status": doc.status,
            "counterparty": str(doc.counterparty_id) if doc.counterparty_id else None,
            "paid": str(services._base_document_paid_amount(doc)),
            "refunded": str(services._returns_cash_refunded(doc)),
            "items": services.returnable_items(doc),
        })


class CashApprovalRequestListView(CompanyBranchRestrictedMixin, generics.ListAPIView):
    """
    Входящие запросы кассы (CASH_PENDING) с фильтрами/поиском.
    """
    serializer_class = serializers_documents.CashApprovalRequestSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["status", "requires_money", "money_doc_type", "document__doc_type", "document__payment_kind"]
    search_fields = ["document__number", "document__comment", "document__counterparty__name"]

    def get_queryset(self):
        company = self._company()
        if company is None:
            return models.CashApprovalRequest.objects.none()

        branch = self._auto_branch()
        qs = (
            models.CashApprovalRequest.objects
            .select_related(
                "document",
                "document__warehouse_from",
                "document__counterparty",
                "document__cash_register",
                "document__payment_category",
                "money_document",
                "decided_by",
            )
            .filter(
                Q(document__warehouse_from__company=company) | Q(document__warehouse_to__company=company)
            )
            .order_by("-requested_at")
        )
        if branch is not None:
            qs = qs.filter(
                Q(document__warehouse_from__branch=branch) | Q(document__warehouse_to__branch=branch)
            )
        return qs


class CashApprovalRequestApproveView(CompanyBranchRestrictedMixin, generics.GenericAPIView):
    serializer_class = serializers_documents.CashApprovalRequestSerializer

    def get_queryset(self):
        company = self._company()
        if company is None:
            return models.CashApprovalRequest.objects.none()

        branch = self._auto_branch()
        qs = (
            models.CashApprovalRequest.objects
            .select_related("document__warehouse_from", "document__warehouse_to")
            .filter(
                Q(document__warehouse_from__company=company) | Q(document__warehouse_to__company=company)
            )
        )
        if branch is not None:
            qs = qs.filter(
                Q(document__warehouse_from__branch=branch) | Q(document__warehouse_to__branch=branch)
            )
        return qs

    def post(self, request, pk=None):
        cash_request = self.get_object()
        ser = serializers_documents.CashApprovalDecisionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        note = (ser.validated_data.get("note") or "").strip()

        try:
            services.approve_cash_request(
                cash_request.document, decided_by=request.user, note=note,
                allow_negative_cash=_truthy(request.data.get("allow_negative_cash")),
            )
        except Exception as e:
            return _service_error_response(e)

        cash_request.refresh_from_db()
        out = serializers_documents.CashApprovalRequestSerializer(cash_request, context={"request": request}).data
        return Response(out, status=status.HTTP_200_OK)


class CashApprovalRequestRejectView(CompanyBranchRestrictedMixin, generics.GenericAPIView):
    serializer_class = serializers_documents.CashApprovalRequestSerializer

    def get_queryset(self):
        company = self._company()
        if company is None:
            return models.CashApprovalRequest.objects.none()

        branch = self._auto_branch()
        qs = (
            models.CashApprovalRequest.objects
            .select_related("document__warehouse_from", "document__warehouse_to")
            .filter(
                Q(document__warehouse_from__company=company) | Q(document__warehouse_to__company=company)
            )
        )
        if branch is not None:
            qs = qs.filter(
                Q(document__warehouse_from__branch=branch) | Q(document__warehouse_to__branch=branch)
            )
        return qs

    def post(self, request, pk=None):
        cash_request = self.get_object()
        ser = serializers_documents.CashApprovalDecisionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        note = (ser.validated_data.get("note") or "").strip()

        try:
            services.reject_cash_request(cash_request.document, decided_by=request.user, note=note)
        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        cash_request.refresh_from_db()
        out = serializers_documents.CashApprovalRequestSerializer(cash_request, context={"request": request}).data
        return Response(out, status=status.HTTP_200_OK)


@read_only_guard("can_edit_documents")
class DocumentTransferCreateAPIView(CompanyBranchRestrictedMixin, APIView):
    """
    Быстрое перемещение товара (создает документ TRANSFER и сразу проводит).
    POST /api/warehouse/transfer/
    """
    def post(self, request, *args, **kwargs):
        ser = serializers_documents.TransferCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        company = self._company()
        branch = self._auto_branch()
        if not company:
            raise DRFValidationError({"company": "Компания не найдена."})
        wh_from = ser.validated_data["warehouse_from"]
        wh_to = ser.validated_data["warehouse_to"]
        self._ensure_agent_can_access_warehouse(wh_from, field_name="warehouse_from")
        self._ensure_agent_can_access_warehouse(wh_to, field_name="warehouse_to")

        if wh_from.company_id != wh_to.company_id:
            raise DRFValidationError(
                {
                    "warehouse": "Межкомпанейское перемещение выполняйте через POST /api/warehouse/stock-partnerships/transfer/ "
                    "(нужно активное партнёрство между компаниями)."
                }
            )

        if company and (wh_from.company_id != company.id or wh_to.company_id != company.id):
            raise DRFValidationError({"warehouse": "Склад принадлежит другой компании."})

        if branch is not None:
            if wh_from.branch_id not in (None, branch.id) or wh_to.branch_id not in (None, branch.id):
                raise DRFValidationError({"warehouse": "Склад другого филиала."})
        else:
            if wh_from.branch_id is not None or wh_to.branch_id is not None:
                raise DRFValidationError({"warehouse": "Склад другого филиала."})

        doc = models.Document.objects.create(
            doc_type=models.Document.DocType.TRANSFER,
            warehouse_from=wh_from,
            warehouse_to=wh_to,
            comment=ser.validated_data.get("comment") or "",
            created_by=request.user,
        )

        for it in ser.validated_data["items"]:
            item = models.DocumentItem(document=doc, **it)
            try:
                item.clean()
            except Exception as e:
                raise DRFValidationError(getattr(e, "message_dict", {"detail": str(e)}))
            item.save()

        try:
            services.post_document(
                doc, user=request.user, allow_negative=_truthy(request.data.get("allow_negative"))
            )
        except Exception as e:
            _raise_service_error(e)

        out = serializers_documents.DocumentSerializer(doc, context={"request": request}).data
        return Response(out, status=status.HTTP_201_CREATED)


class GenerateBarcodeView(CompanyBranchRestrictedMixin, APIView):
    """
    QA B30: следующий свободный внутренний штрихкод EAN-13 (префикс 20) компании.
    GET /api/warehouse/products/generate-barcode/ → {"barcode": "2000000000017"}
    """

    def get(self, request, *args, **kwargs):
        from .validators import generate_internal_barcode

        company = self._company()
        if company is None:
            raise DRFValidationError({"company": "Компания не найдена."})
        return Response({"barcode": generate_internal_barcode(company)})


@read_only_guard("can_edit_products")
class ProductListCreateView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = serializers_documents.ProductSimpleSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_class = ProductFilter
    pagination_class = ProductCatalogPagination
    
    def get_queryset(self):
        # Оптимизация: предзагружаем связанные объекты
        qs = (
            models.WarehouseProduct.objects.select_related(
                "warehouse", "brand", "category", "company", "branch", "product_group", "supplier"
            )
            .prefetch_related("alternate_barcodes")
        )
        qs = self._filter_qs_company_branch(qs)

        wh_id = self.request.query_params.get("warehouse") or self.request.query_params.get("warehouse_id")
        if wh_id:
            qs = qs.filter(warehouse_id=wh_id)

        # Кэширование поиска по barcode
        search = self.request.query_params.get("search", "").strip()
        if search and len(search) >= 8:  # Предполагаем, что barcode обычно длиннее 8 символов
            from django.core.cache import cache
            company = self._company()
            if company:
                cache_key = f"warehouse_product_barcode:{company.id}:{search}"
                cached_product_id = cache.get(cache_key)
                if cached_product_id:
                    # Если найден в кэше - возвращаем только этот товар
                    return qs.filter(pk=cached_product_id)
                # Ищем товар и кэшируем его ID
                product = qs.filter(Q(barcode=search) | Q(alternate_barcodes__barcode=search)).distinct().first()
                if product:
                    cache.set(cache_key, product.id, 300)  # Кэш на 5 минут
        
        return qs


@read_only_guard("can_edit_products")
class ProductDetailView(ProtectedProductDeleteMixin, CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = serializers_documents.ProductSimpleSerializer

    def get_queryset(self):
        # Оптимизация: предзагружаем связанные объекты
        qs = (
            models.WarehouseProduct.objects.select_related(
                "warehouse", "brand", "category", "company", "branch", "supplier"
            )
            .prefetch_related("alternate_barcodes")
        )
        return self._filter_qs_company_branch(qs)


class WarehouseListCreateView(WarehouseGuardMixin, CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = serializers_documents.WarehouseSimpleSerializer
    
    def get_queryset(self):
        # Оптимизация: предзагружаем связанные объекты
        qs = models.Warehouse.objects.select_related("company", "branch")
        return self._filter_qs_company_branch(qs)


class WarehouseDetailView(WarehouseGuardMixin, CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = serializers_documents.WarehouseSimpleSerializer
    
    def get_queryset(self):
        # Оптимизация: предзагружаем связанные объекты
        qs = models.Warehouse.objects.select_related("company", "branch")
        return self._filter_qs_company_branch(qs)


class CounterpartyPagination(PageNumberPagination):
    """
    Пагинация для контрагентов (Склад -> Вкладка Контрагенты).
    Позволяет передавать ?page_size= (до 10000) или ?page_size=all / 0 / -1.
    По умолчанию отдаёт 1000 контрагентов на страницу (вместо системного ограничения в 100),
    чтобы в списке на фронтенде отображались все контрагенты компании/агента (100+).
    """
    page_size = 1000
    page_size_query_param = "page_size"
    max_page_size = 10000

    def get_page_size(self, request):
        if self.page_size_query_param:
            val = request.query_params.get(self.page_size_query_param)
            if val in ("all", "0", "-1"):
                return self.max_page_size
        return super().get_page_size(request)


# Значения, включающие режим «только не оплатившие» (должники).
_UNPAID_FLAGS = {"1", "true", "yes", "unpaid", "debt", "debtors", "not_paid"}


def _wants_only_unpaid(request) -> bool:
    qp = getattr(request, "query_params", None) or {}
    raw = (
        qp.get("only_unpaid")
        or qp.get("unpaid")
        or qp.get("payment_status")
        or ""
    ).strip().lower()
    return raw in _UNPAID_FLAGS


class CounterpartyListCreateView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    queryset = models.Counterparty.objects.all()
    serializer_class = serializers_documents.CounterpartySerializer
    pagination_class = CounterpartyPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["agent", "type"]
    search_fields = ["name", "phone"]

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        m = getattr(self, "_counterparty_analytics_map", None)
        if m is not None:
            ctx["counterparty_analytics_map"] = m
        return ctx

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        try:
            if page is not None:
                self._counterparty_analytics_map = services_money.bulk_counterparty_mini_analytics(
                    self, [o.pk for o in page]
                )
                serializer = self.get_serializer(page, many=True)
                return self.get_paginated_response(serializer.data)
            self._counterparty_analytics_map = services_money.bulk_counterparty_mini_analytics(
                self, [o.pk for o in queryset]
            )
            serializer = self.get_serializer(queryset, many=True)
            return Response(serializer.data)
        finally:
            self._counterparty_analytics_map = None

    def _unpaid_counterparty_ids(self, on_date):
        """
        Контрагенты с непогашенным долгом на дату: сальдо на конец периода
        по дебету больше, чем по кредиту (трактовка — как в акте сверки).
        """
        period_map = services_money.counterparty_period_balances(
            self, date_from=on_date, date_to=on_date, per_counterparty=True,
        )
        return [
            cid for cid, row in period_map.items()
            if (row["closing_debit"] - row["closing_credit"]) > 0
        ]

    def get_queryset(self):
        qs = filter_qs_company_branch_or_global(self, models.Counterparty.objects.all())
        user = self.request.user
        if not _is_owner_like(user):
            qs = qs.filter(agent=user)
        date_from, date_to = services_money.get_requested_date_range(self)
        only_unpaid = _wants_only_unpaid(self.request)
        if only_unpaid:
            # Режим одного дня: показываем только тех, кто не оплатил (есть долг
            # на конец выбранной даты), включая тех, у кого в этот день движений не было.
            on_date = date_to or date_from or timezone.localdate()
            return qs.filter(pk__in=self._unpaid_counterparty_ids(on_date)).distinct()
        # Период НЕ сужает состав списка: страница «Контрагенты» — это ещё и
        # справочник, и контрагент без документов должен остаться в нём с нулями
        # (иначе при выборе месяца из таблицы пропадают сотни живых клиентов).
        # Суммы строк при этом сходятся с карточками: нулевые строки ничего не
        # добавляют. Период влияет только на цифры в строках, а состав сужает
        # лишь режим одного дня (only_unpaid).
        return qs

    def perform_create(self, serializer):
        user = self.request.user
        # B16: дубли по телефону/ИНН запрещены, по имени — только с force_duplicate_name.
        ensure_unique_counterparty(
            company=self._company(),
            data=serializer.validated_data,
            force_duplicate_name=_truthy(self.request.data.get("force_duplicate_name")),
        )
        if _is_owner_like(user):
            company = self._company()
            agent = serializer.validated_data.get("agent")
            if agent and company and not _agent_allowed_for_company(agent, company):
                raise DRFValidationError(
                    {"agent": "Агент должен быть сотрудником или активным агентом этой компании."}
                )
            self._save_with_company_branch(serializer)
            return
        self._save_with_company_branch(serializer, agent=user)


class CounterpartyDetailView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    queryset = models.Counterparty.objects.all()
    serializer_class = serializers_documents.CounterpartySerializer

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        if self.request.method == "GET" and self.kwargs.get("pk"):
            ctx["counterparty_analytics_map"] = services_money.bulk_counterparty_mini_analytics(
                self, [self.kwargs["pk"]]
            )
        return ctx

    def get_queryset(self):
        qs = filter_qs_company_branch_or_global(self, models.Counterparty.objects.all())
        user = self.request.user
        if not _is_owner_like(user):
            qs = qs.filter(agent=user)
        return qs

    def perform_update(self, serializer):
        user = self.request.user
        instance = serializer.instance
        ensure_unique_counterparty(
            company=instance.company or self._company(),
            data=serializer.validated_data,
            instance=instance,
            force_duplicate_name=_truthy(self.request.data.get("force_duplicate_name")),
        )
        if _is_owner_like(user):
            company = getattr(instance, "company", None) or self._company()
            agent = serializer.validated_data.get("agent")
            if agent is not None and company and not _agent_allowed_for_company(agent, company):
                raise DRFValidationError(
                    {"agent": "Агент должен быть сотрудником или активным агентом этой компании."}
                )
            self._save_with_company_branch(serializer)
            return
        serializer.validated_data["agent"] = getattr(instance, "agent", None) or user
        serializer.save()


class CounterpartyBalanceSummaryView(CompanyBranchRestrictedMixin, APIView):
    """
    GET /api/warehouse/counterparties/balance-summary/

    Сводка по контрагентам за период: сальдо на начало, оборот, сальдо на конец —
    каждое с разбивкой на дебет и кредит. Агрегат по всем контрагентам выбранного
    типа в рамках компании пользователя. Дебет/кредит — как в акте сверки.

    Query: type=client|supplier (опц.), date_from=YYYY-MM-DD, date_to=YYYY-MM-DD.
    """

    permission_classes = [permissions.IsAuthenticated]

    _TYPE_MAP = {
        "client": models.Counterparty.Type.CLIENT,
        "supplier": models.Counterparty.Type.SUPPLIER,
    }

    def get(self, request, *args, **kwargs):
        from django.utils.dateparse import parse_date

        date_from = parse_date(request.query_params.get("date_from") or "")
        date_to = parse_date(request.query_params.get("date_to") or "")
        if not date_from or not date_to:
            return Response(
                {"detail": "Укажите параметры date_from и date_to (YYYY-MM-DD)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if date_to < date_from:
            date_from, date_to = date_to, date_from

        type_raw = (request.query_params.get("type") or "").strip().lower()
        counterparty_type = None
        if type_raw and type_raw != "both":
            counterparty_type = self._TYPE_MAP.get(type_raw)
            if counterparty_type is None:
                return Response(
                    {"detail": "Параметр type должен быть client или supplier."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        counterparty_ids = None

        # Фильтр по агенту — тот же, что в списке контрагентов (?agent=<uuid>),
        # иначе карточки считаются по всем контрагентам и не сходятся с таблицей.
        # Агент (не владелец) видит итоги только по своим контрагентам.
        agent_raw = (request.query_params.get("agent") or "").strip()
        user = request.user
        if not _is_owner_like(user):
            agent_raw = ""  # не-владелец не может подставить чужого агента
        if agent_raw or not _is_owner_like(user):
            agent_qs = filter_qs_company_branch_or_global(self, models.Counterparty.objects.all())
            if counterparty_type:
                agent_qs = agent_qs.filter(type=counterparty_type)
            agent_qs = agent_qs.filter(agent_id=agent_raw or user.pk)
            try:
                counterparty_ids = list(agent_qs.values_list("id", flat=True))
            except (ValueError, DjangoValidationError):
                return Response(
                    {"detail": "Параметр agent должен быть UUID."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if _wants_only_unpaid(request):
            # Тот же режим, что и в списке контрагентов: только не оплатившие
            # (сальдо на конец периода по дебету больше, чем по кредиту).
            per_cp = services_money.counterparty_period_balances(
                self, date_from=date_to, date_to=date_to,
                counterparty_type=counterparty_type, counterparty_ids=counterparty_ids,
                per_counterparty=True,
            )
            counterparty_ids = [
                cid for cid, r in per_cp.items()
                if (r["closing_debit"] - r["closing_credit"]) > 0
            ]

        row = services_money.counterparty_period_balances(
            self, date_from=date_from, date_to=date_to,
            counterparty_type=counterparty_type, counterparty_ids=counterparty_ids,
            per_counterparty=False,
        )
        # net = дебет − кредит: «сколько в итоге должен». Дебет — начислено
        # (отгрузки, выдачи денег), кредит — погашено (оплаты, возвраты).
        # Положительный net — должен контрагент, отрицательный — переплата.
        def _block(prefix):
            debit = row[f"{prefix}_debit"]
            credit = row[f"{prefix}_credit"]
            net = debit - credit
            return {"debit": str(debit), "credit": str(credit), "net": str(net)}

        closing_net = row["closing_debit"] - row["closing_credit"]
        return Response({
            "opening": _block("opening"),
            "turnover": _block("turnover"),
            "closing": _block("closing"),
            "totals": {
                "debt_total": str(row["opening_debit"] + row["turnover_debit"]),
                "paid_total": str(row["opening_credit"] + row["turnover_credit"]),
                "debt_remaining": str(closing_net),
                # Стороны берём как есть, без свёртки между контрагентами:
                # долг одного не гасится переплатой другого (§2.3 ТЗ).
                "counterparties_owe": str(row["closing_debit"]),
                "company_owes": str(row["closing_credit"]),
            },
        }, status=status.HTTP_200_OK)
