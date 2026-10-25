"""Этап 3: договоры и шаблоны (ТЗ §3.3, §3.7): шаблоны, мастер договора аренды, документы, трудовые договоры."""
import os
import re
from urllib.parse import quote

from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.models import Branch
from core.exceptions import ApiError

from . import audit
from .branch_filter import apply_branch, branch_ids_from_request
from .models import Counter, Employee, EmploymentContract, Lease, Template, Tenant, Unit
from .permissions import ArendaSection, deny
from .serializers import TenantInSerializer
from .serializers_docs import TemplateInSerializer, TermsSerializer
from .services import docx_io
from .services import templates as eng
from .services.charges import ensure_rent_charge
from .services.money import calc_rent
from .views import CtxMixin, ManualPaging, find_overlaps, lease_payload

class IgnoreFormatNegotiation(DefaultContentNegotiation):
    """?format=pdf|docx здесь — наш параметр, а не выбор рендерера DRF."""

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


KINDS = ("lease", "employment")
DOC_SECTIONS = ("leases", "staff")
MAX_UPLOAD = 2 * 1024 * 1024
FORM_TITLES = dict(Tenant.FORMS)


# ---------------------------------------------------------------- общее


def _used_count(t):
    return (Lease.objects.filter(template=t).count()
            + EmploymentContract.objects.filter(template=t).count())


def template_payload(t, used=None):
    return {
        "id": str(t.id), "kind": t.kind, "name": t.name, "text": t.text,
        "archived": t.archived_at is not None, "archived_at": t.archived_at.isoformat() if t.archived_at else None,
        "used_count": _used_count(t) if used is None else used,
        "created_at": t.created_at.isoformat(), "updated_at": t.updated_at.isoformat(),
    }


def _ascii_name(label, ext):
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", label.replace("№", "").replace("А", "A").replace("Т", "T")).strip("-")
    return f"contract-{base or 'doc'}.{ext}"


def file_response(fmt, blocks, label, request, title):
    if fmt == "pdf":
        data, ctype = docx_io.build_pdf(blocks, title=title), "application/pdf"
    else:
        data = docx_io.build_docx(blocks)
        ctype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    resp = HttpResponse(data, content_type=ctype)
    disp = "inline" if request.query_params.get("inline") in ("1", "true") else "attachment"
    resp["Content-Disposition"] = (f'{disp}; filename="{_ascii_name(label, fmt)}"; '
                                   f"filename*=UTF-8''{quote(f'{title}.{fmt}')}")
    return resp


def document_response(request, fmt, *, label, snapshot, title):
    """fmt: None -> JSON/HTML по ?format=, pdf|docx -> файл."""
    fmt = fmt or request.query_params.get("format") or "json"
    if not snapshot:
        raise ApiError("no_snapshot", "У договора нет сохранённого текста (создан без шаблона).", status_code=409)
    blocks = eng.to_blocks(snapshot)
    if fmt in ("pdf", "docx"):
        return file_response(fmt, blocks, label, request, title)
    html = eng.snapshot_html(snapshot)
    if fmt == "html":
        page = (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{title}</title></head>'
                f'<body style="max-width:800px;margin:2em auto">{html}</body></html>')
        return HttpResponse(page, content_type="text/html; charset=utf-8")
    return Response({"label": label, "title": title, "html": html, "text": snapshot})


def _branch_city(branch):
    info = getattr(branch, "arenda_info", None) if branch is not None else None
    return (info.city if info else "") or ""


def _apply_overrides(values, overrides):
    if overrides is None:
        return values
    if not isinstance(overrides, dict):
        raise ValidationError({"overrides": ["Ожидается объект {поле: значение}."]})
    for k, v in overrides.items():
        values[eng.canonical_key(str(k), "lease")] = "" if v is None else str(v)
    return values


def _pick_template(ctx, kind, template_id):
    if template_id:
        t = Template.objects.filter(pk=template_id, company=ctx.company, kind=kind).first() \
            if _is_uuid(template_id) else None
        if not t:
            raise ApiError("not_found", "Шаблон не найден.", status_code=404)
        if t.archived_at:
            raise ApiError("template_archived", "Шаблон в архиве.", status_code=409)
        return t
    eng.ensure_default_templates(ctx.company, kind)
    t = Template.objects.filter(company=ctx.company, kind=kind, archived_at__isnull=True).order_by("created_at").first()
    if not t:
        raise ValidationError({"template_id": ["Нет доступного шаблона — создайте или верните из архива."]})
    return t


def _is_uuid(v):
    import uuid
    try:
        uuid.UUID(str(v))
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------- шаблоны


class TemplateListView(ManualPaging, CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection(*DOC_SECTIONS)]

    def get(self, request):
        p = request.query_params
        kind = p.get("kind")
        if kind and kind not in KINDS:
            raise ValidationError({"kind": ["lease или employment."]})
        eng.ensure_default_templates(self.ctx.company, kind)
        qs = Template.objects.filter(company=self.ctx.company)
        if kind:
            qs = qs.filter(kind=kind)
        arch = p.get("archived", "0")
        if arch in ("1", "true"):
            qs = qs.filter(archived_at__isnull=False)
        elif arch != "all":
            qs = qs.filter(archived_at__isnull=True)
        if p.get("search"):
            qs = qs.filter(name__icontains=p["search"])
        page = self.paginate_queryset([template_payload(t) for t in qs.order_by("kind", "created_at")])
        return self.get_paginated_response(page)

    def post(self, request):
        ser = TemplateInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        text = eng.normalize_template_text(d["text"], d["kind"])
        t = Template.objects.create(company=self.ctx.company, created_by=request.user, kind=d["kind"],
                                    name=d["name"].strip(), text=text)
        audit.log(request, self.ctx.company, "create", "template", t, after={"name": t.name, "kind": t.kind})
        return Response(template_payload(t, 0), status=201)


class TemplateDetailView(CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection(*DOC_SECTIONS)]

    def _get(self, pk):
        t = Template.objects.filter(pk=pk, company=self.ctx.company).first() if _is_uuid(pk) else None
        if not t:
            raise ApiError("not_found", "Шаблон не найден.", status_code=404)
        return t

    def get(self, request, pk):
        return Response(template_payload(self._get(pk)))

    def patch(self, request, pk):
        t = self._get(pk)
        before = {"name": t.name, "archived": t.archived_at is not None}
        d = request.data
        if "name" in d:
            name = str(d["name"] or "").strip()
            if not name:
                raise ValidationError({"name": ["Укажите название."]})
            t.name = name[:255]
        if "text" in d:
            if len(str(d["text"] or "")) > 300000:
                raise ValidationError({"text": ["Слишком длинный текст."]})
            t.text = eng.normalize_template_text(str(d["text"] or ""), t.kind)
        if "archived" in d:
            flag = d["archived"] in (True, "true", "1", 1)
            t.archived_at = (t.archived_at or timezone.now()) if flag else None
        t.save()
        audit.log(request, self.ctx.company, "update", "template", t, before=before,
                  after={"name": t.name, "archived": t.archived_at is not None})
        return Response(template_payload(t))

    put = patch

    def delete(self, request, pk):
        """Неиспользуемый шаблон удаляется; используемый — 409, нужен архив (?archive=1)."""
        t = self._get(pk)
        used = _used_count(t)
        if request.query_params.get("archive") in ("1", "true"):
            t.archived_at = t.archived_at or timezone.now()
            t.save(update_fields=["archived_at", "updated_at"])
            audit.log(request, self.ctx.company, "archive", "template", t)
            return Response(template_payload(t, used))
        if used:
            raise ApiError("template_in_use",
                           "По шаблону есть договоры, удалить нельзя. Отправьте его в архив "
                           "(DELETE ?archive=1 или POST .../archive/).",
                           status_code=409, data={"used_count": used, "archive": True})
        audit.log(request, self.ctx.company, "delete", "template", t, before={"name": t.name, "kind": t.kind})
        t.delete()
        return Response(status=204)


class TemplateArchiveView(TemplateDetailView):
    def post(self, request, pk):
        t = self._get(pk)
        t.archived_at = t.archived_at or timezone.now()
        t.save(update_fields=["archived_at", "updated_at"])
        audit.log(request, self.ctx.company, "archive", "template", t)
        return Response(template_payload(t))


class TemplateCopyView(TemplateDetailView):
    def post(self, request, pk):
        src = self._get(pk)
        t = Template.objects.create(company=self.ctx.company, created_by=request.user, kind=src.kind,
                                    name=f"Копия — {src.name}"[:255], text=src.text)
        audit.log(request, self.ctx.company, "create", "template", t, after={"name": t.name, "copy_of": str(src.id)})
        return Response(template_payload(t, 0), status=201)


class TemplateFieldsView(CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection(*DOC_SECTIONS)]

    def get(self, request):
        kind = request.query_params.get("kind", "lease")
        if kind not in KINDS:
            raise ValidationError({"kind": ["lease или employment."]})
        return Response({"kind": kind, "fields": eng.fields_for(kind), "special_lines": eng.SPECIAL_LINES})


class TemplateImportView(CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection(*DOC_SECTIONS)]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        f = request.FILES.get("file")
        if not f:
            raise ValidationError({"file": ["Приложите файл .docx или .txt."]})
        kind = request.data.get("kind") or "lease"
        if kind not in KINDS:
            raise ValidationError({"kind": ["lease или employment."]})
        if f.size > MAX_UPLOAD:
            raise ValidationError({"file": ["Файл больше 2 МБ."]})
        ext = os.path.splitext(f.name or "")[1].lower()
        raw = f.read()
        if ext == ".docx":
            try:
                text = docx_io.parse_docx(raw)
            except docx_io.DocxError as e:
                raise ValidationError({"file": [str(e)]})
        elif ext == ".txt":
            for enc in ("utf-8-sig", "cp1251"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                raise ValidationError({"file": ["Не удалось определить кодировку."]})
        else:
            raise ValidationError({"file": ["Поддерживаются только .docx и .txt."]})
        text = eng.normalize_template_text(text.replace("\r\n", "\n"), kind)
        name = (request.data.get("name") or os.path.splitext(os.path.basename(f.name))[0] or "Импортированный").strip()
        known, unknown = eng.used_fields(text, kind)
        t = Template.objects.create(company=self.ctx.company, created_by=request.user, kind=kind, name=name[:255],
                                    text=text)
        audit.log(request, self.ctx.company, "create", "template", t,
                  after={"name": t.name, "kind": kind, "imported": ext})
        return Response({**template_payload(t, 0), "fields_found": known, "unknown_fields": unknown}, status=201)


# ---------------------------------------------------------------- мастер договора аренды


def _tenant_dict(t):
    return {"name": t.name, "form": t.form, "inn": t.inn, "contact_person": t.contact_person,
            "passport": t.passport, "phone": t.phone, "email": t.email, "activity": t.activity}


class LeaseWizardMixin:
    """Общая подготовка для preview/sign: проверки, помещения, арендатор, условия."""

    def prepare(self, request, strict):
        ctx = self.ctx
        data = request.data
        raw_ids = data.get("unit_ids", data.get("units")) or []
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValidationError({"unit_ids": ["Выберите помещение."]})
        if not all(_is_uuid(i) for i in raw_ids):
            raise ValidationError({"unit_ids": ["Некорректный идентификатор помещения."]})
        units = list(Unit.objects.filter(pk__in=raw_ids, company=ctx.company, archived_at__isnull=True)
                     .select_related("branch"))
        if len(units) != len(set(raw_ids)):
            raise ValidationError({"unit_ids": ["Помещение не найдено."]})
        branch = units[0].branch
        if len({u.branch_id for u in units}) > 1:
            raise ValidationError({"unit_ids": ["Помещения должны быть из одного филиала."]})
        chosen = data.get("branch")
        if chosen and str(chosen) != "all" and str(chosen) != str(branch.id):
            raise ApiError("unit_not_free", "Помещение не из выбранного филиала.", status_code=409,
                           data={"units": [str(u.id) for u in units]})
        allowed = ctx.allowed_branch_ids()
        if allowed is not None and branch.id not in allowed:
            raise deny("leases")

        terms_in = data.get("terms")
        if terms_in is None:
            terms_in = {k: data[k] for k in TermsSerializer().fields if k in data}
        ts = TermsSerializer(data=terms_in)
        ts.is_valid(raise_exception=True)
        terms = dict(ts.validated_data)
        if strict and not terms.get("start_date"):
            raise ValidationError({"terms": {"start_date": ["Укажите дату начала."]}})

        tenant_obj, tenant_dict, tenant_ser = None, {}, None
        if data.get("tenant_id"):
            tenant_obj = (Tenant.objects.filter(pk=data["tenant_id"], company=ctx.company, archived_at__isnull=True)
                          .first() if _is_uuid(data["tenant_id"]) else None)
            if not tenant_obj:
                raise ApiError("not_found", "Арендатор не найден.", status_code=404)
            tenant_dict = _tenant_dict(tenant_obj)
        else:
            raw_t = data.get("tenant") or {}
            if not isinstance(raw_t, dict):
                raise ValidationError({"tenant": ["Ожидается объект."]})
            tenant_ser = TenantInSerializer(data={**raw_t, "branch": str(branch.id)}, partial=not strict,
                                            context={"ctx": ctx})
            tenant_ser.is_valid(raise_exception=True)
            tenant_dict = {k: (v if isinstance(v, str) else "") for k, v in tenant_ser.validated_data.items()
                           if k != "branch"}
            tenant_dict.setdefault("form", "ip")
            if strict and not tenant_dict.get("name", "").strip():
                raise ValidationError({"tenant": {"name": ["Укажите арендатора."]}})
        template = _pick_template(ctx, "lease", data.get("template_id"))
        on_date = timezone.localdate()
        if data.get("date"):
            try:
                from datetime import date as _d
                on_date = _d.fromisoformat(str(data["date"]))
            except ValueError:
                raise ValidationError({"date": ["Формат YYYY-MM-DD."]})
        return {"units": units, "branch": branch, "terms": terms, "tenant_obj": tenant_obj,
                "tenant": tenant_dict, "tenant_ser": tenant_ser, "template": template, "on_date": on_date,
                "overrides": data.get("overrides")}

    def check_free(self, prep):
        start = prep["terms"].get("start_date") or timezone.localdate()
        bad = find_overlaps([u.pk for u in prep["units"]], start, prep["terms"].get("end_date"))
        if bad:
            raise ApiError("lease_overlap", "Помещение уже сдано на эти даты.", status_code=409, data={"units": bad})

    def values(self, prep, label):
        v = eng.lease_values(
            settings_obj=self.settings_obj(), branch=prep["branch"], city=_branch_city(prep["branch"]),
            tenant=prep["tenant"], units=prep["units"], terms=prep["terms"], label=label, on_date=prep["on_date"],
            tenant_form_title=FORM_TITLES.get(prep["tenant"].get("form", "ip"), ""))
        return _apply_overrides(v, prep["overrides"])


class LeasePreviewView(LeaseWizardMixin, CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection("leases")]

    def post(self, request):
        prep = self.prepare(request, strict=False)
        self.check_free(prep)
        nxt = Counter.objects.filter(company=self.ctx.company, key="lease").values_list("last_value", flat=True).first() or 0
        label = f"А-{prep['on_date'].year}/{nxt + 1:03d}"
        values = self.values(prep, label)
        r = eng.render_template(prep["template"].text, values, "lease")
        labels = {f["key"]: f["label"] for f in eng.fields_for("lease")}
        return Response({
            "label": label, "template": str(prep["template"].id), "html": r["html"], "text": r["text"],
            "filled": r["filled"], "missing": r["missing"], "unknown": r["unknown"],
            "labels": {k: labels.get(k, k) for k in r["filled"] + r["missing"]},
            "values": {k: v for k, v in values.items() if k in r["filled"]},
        })


class LeaseSignView(LeaseWizardMixin, CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection("leases")]

    def post(self, request):
        ctx = self.ctx
        prep = self.prepare(request, strict=True)
        terms, units = prep["terms"], prep["units"]
        with transaction.atomic():
            list(Unit.objects.select_for_update().filter(pk__in=[u.pk for u in units]))
            self.check_free(prep)
            tenant = prep["tenant_obj"]
            if tenant is None:
                tenant = prep["tenant_ser"].save(company=ctx.company, created_by=request.user, branch=prep["branch"])
                audit.log(request, ctx.company, "create", "tenant", tenant, after={"name": tenant.name},
                          branch=prep["branch"])
            n = Counter.next(ctx.company, "lease")
            label = f"А-{prep['on_date'].year}/{n:03d}"
            values = self.values(prep, label)
            r = eng.render_template(prep["template"].text, values, "lease")
            s = self.settings_obj()
            monthly = terms.get("rent_monthly")
            base = monthly if monthly is not None else sum(calc_rent(u.area, u.rate) for u in units)
            dm = terms.get("deposit_months", 0)
            lease = Lease.objects.create(
                company=ctx.company, created_by=request.user, branch=prep["branch"], number=n, label=label,
                tenant=tenant, template=prep["template"], start_date=terms["start_date"],
                end_date=terms.get("end_date"), rent_monthly=monthly, pay_day=terms.get("pay_day", 10),
                deposit_months=dm,
                deposit_amount=terms["deposit_amount"] if terms.get("deposit_amount") is not None else base * dm,
                penalty_rate=terms["penalty_rate"] if terms.get("penalty_rate") is not None else s.penalty_rate,
                indexation_percent=terms.get("indexation_percent", 0), utilities=terms.get("utilities", ""),
                status="active", text_snapshot=r["text"], signed_at=timezone.now(),
            )
            lease.units.set(units)
            ensure_rent_charge(lease, timezone.localdate(), request.user)
            audit.log(request, ctx.company, "sign", "lease", lease, after={
                "label": label, "tenant": str(tenant.pk), "template": str(prep["template"].id),
                "start": str(lease.start_date), "end": str(lease.end_date)}, branch=prep["branch"])
        lease = Lease.objects.select_related("tenant").prefetch_related("units").get(pk=lease.pk)
        return Response({
            "lease": lease_payload(lease, timezone.localdate(), s.lease_expiring_days),
            "label": label, "html": r["html"], "text": r["text"],
            "filled": r["filled"], "missing": r["missing"], "unknown": r["unknown"],
        }, status=201)


# ---------------------------------------------------------------- документы договора аренды


class LeaseDocumentView(CtxMixin, APIView):
    content_negotiation_class = IgnoreFormatNegotiation
    permission_classes = [IsAuthenticated, ArendaSection("leases")]

    def get(self, request, pk, fmt=None):
        qs = Lease.objects.filter(pk=pk, company=self.ctx.company, archived_at__isnull=True)
        allowed = self.ctx.allowed_branch_ids()
        if allowed is not None:
            qs = qs.filter(branch_id__in=allowed)
        lease = qs.first()
        if not lease:
            raise ApiError("not_found", "Договор не найден.", status_code=404)
        return document_response(request, fmt, label=lease.label, snapshot=lease.text_snapshot,
                                 title=f"Договор аренды {lease.label}")


# ---------------------------------------------------------------- трудовые договоры


class EmployeeContractMixin:
    def employee(self, pk):
        qs = Employee.objects.filter(pk=pk, company=self.ctx.company, archived_at__isnull=True).select_related("branch")
        allowed = self.ctx.allowed_branch_ids()
        if allowed is not None:
            qs = qs.filter(branch_id__in=allowed)
        e = qs.first()
        if not e:
            raise ApiError("not_found", "Сотрудник не найден.", status_code=404)
        return e

    def build(self, request, emp, label, on_date):
        template = _pick_template(self.ctx, "employment", request.data.get("template_id"))
        values = eng.employment_values(settings_obj=self.settings_obj(), branch=emp.branch,
                                       city=_branch_city(emp.branch), employee=emp, label=label, on_date=on_date)
        _apply_overrides(values, request.data.get("overrides"))
        return template, eng.render_template(template.text, values, "employment")

    def on_date(self, request):
        raw = request.data.get("date")
        if not raw:
            return timezone.localdate()
        try:
            from datetime import date as _d
            return _d.fromisoformat(str(raw))
        except ValueError:
            raise ValidationError({"date": ["Формат YYYY-MM-DD."]})


class EmployeeContractPreviewView(EmployeeContractMixin, CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection(*DOC_SECTIONS)]

    def post(self, request, pk):
        emp = self.employee(pk)
        d = self.on_date(request)
        nxt = Counter.objects.filter(company=self.ctx.company, key="employment").values_list("last_value", flat=True).first() or 0
        label = f"Т-{d.year}/{nxt + 1:03d}"
        template, r = self.build(request, emp, label, d)
        return Response({"label": label, "template": str(template.id), "html": r["html"], "text": r["text"],
                         "filled": r["filled"], "missing": r["missing"], "unknown": r["unknown"]})


def contract_payload(c):
    e = c.employee
    return {
        "id": str(c.id), "number": c.number, "label": c.label, "date": c.date.isoformat(),
        "employee": str(e.id), "employee_name": e.full_name, "position": e.position, "salary": e.salary,
        "branch": str(e.branch_id), "template": str(c.template_id) if c.template_id else None,
        "template_name": c.template.name if c.template_id else None, "has_snapshot": bool(c.text_snapshot),
    }


class EmployeeContractSignView(EmployeeContractMixin, CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection(*DOC_SECTIONS)]

    def post(self, request, pk):
        emp = self.employee(pk)
        d = self.on_date(request)
        with transaction.atomic():
            n = Counter.next(self.ctx.company, "employment")
            label = f"Т-{d.year}/{n:03d}"
            template, r = self.build(request, emp, label, d)
            c = EmploymentContract.objects.create(
                company=self.ctx.company, created_by=request.user, employee=emp, template=template, number=n,
                label=label, date=d, text_snapshot=r["text"])
            audit.log(request, self.ctx.company, "sign", "employment_contract", c,
                      after={"label": label, "employee": str(emp.id)}, branch=emp.branch)
        c = EmploymentContract.objects.select_related("employee", "template").get(pk=c.pk)
        return Response({"contract": contract_payload(c), "label": label, "html": r["html"], "text": r["text"],
                         "filled": r["filled"], "missing": r["missing"], "unknown": r["unknown"]}, status=201)


class EmploymentContractListView(ManualPaging, CtxMixin, APIView):
    permission_classes = [IsAuthenticated, ArendaSection(*DOC_SECTIONS)]

    def get(self, request):
        qs = (EmploymentContract.objects.filter(company=self.ctx.company, archived_at__isnull=True)
              .select_related("employee", "template"))
        qs = apply_branch(qs, branch_ids_from_request(self.ctx, request), "employee__branch_id")
        p = request.query_params
        if p.get("employee"):
            qs = qs.filter(employee_id=p["employee"]) if _is_uuid(p["employee"]) else qs.none()
        if p.get("search"):
            qs = qs.filter(employee__full_name__icontains=p["search"]) | qs.filter(label__icontains=p["search"])
        page = self.paginate_queryset([contract_payload(c) for c in qs.order_by("-date", "-number")])
        return self.get_paginated_response(page)


class EmploymentContractDocumentView(CtxMixin, APIView):
    content_negotiation_class = IgnoreFormatNegotiation
    permission_classes = [IsAuthenticated, ArendaSection(*DOC_SECTIONS)]

    def get(self, request, pk, fmt=None):
        qs = EmploymentContract.objects.filter(pk=pk, company=self.ctx.company, archived_at__isnull=True)
        allowed = self.ctx.allowed_branch_ids()
        if allowed is not None:
            qs = qs.filter(employee__branch_id__in=allowed)
        c = qs.first()
        if not c:
            raise ApiError("not_found", "Договор не найден.", status_code=404)
        return document_response(request, fmt, label=c.label, snapshot=c.text_snapshot,
                                 title=f"Трудовой договор {c.label}")


# ---------------------------------------------------------------- единая база договоров


class ContractsBaseView(ManualPaging, CtxMixin, APIView):
    """База договоров: ?type=lease|employment&status=active|expiring|expired|draft&search=."""
    permission_classes = [IsAuthenticated, ArendaSection(*DOC_SECTIONS)]

    def get(self, request):
        p = request.query_params
        typ = p.get("type")
        if typ and typ not in KINDS:
            raise ValidationError({"type": ["lease или employment."]})
        status = p.get("status")
        if status and status not in ("active", "expiring", "expired", "draft", "terminated"):
            raise ValidationError({"status": ["active, expiring, expired, draft или terminated."]})
        ids = branch_ids_from_request(self.ctx, request)
        sections = self.ctx.sections
        today = timezone.localdate()
        exp_days = self.settings_obj().lease_expiring_days
        search = (p.get("search") or "").strip().lower()
        rows = []
        if typ in (None, "lease") and "leases" in sections:
            qs = (Lease.objects.filter(company=self.ctx.company, archived_at__isnull=True)
                  .select_related("tenant", "template").prefetch_related("units"))
            for l in apply_branch(qs, ids):
                pl = lease_payload(l, today, exp_days)
                nums = " ".join(u["number"] for u in pl["units"])
                if search and search not in f"{l.tenant.name} {l.label} {nums}".lower():
                    continue
                if status and pl["status_display"] != status:
                    continue
                rows.append({"type": "lease", "id": pl["id"], "label": l.label, "party": l.tenant.name,
                             "subject": ", ".join(u["number"] for u in pl["units"]),
                             "start_date": pl["start_date"], "end_date": pl["end_date"],
                             "amount": pl["rent_monthly_calc"], "template": pl["template"],
                             "template_name": l.template.name if l.template_id else None,
                             "status": pl["status_display"], "has_snapshot": pl["has_snapshot"],
                             "branch": pl["branch"], "sort": pl["start_date"]})
        if typ in (None, "employment") and "staff" in sections:
            qs = (EmploymentContract.objects.filter(company=self.ctx.company, archived_at__isnull=True)
                  .select_related("employee", "template"))
            for c in apply_branch(qs, ids, "employee__branch_id"):
                e = c.employee
                st = "expired" if e.fired_at and e.fired_at <= today else "active"
                if search and search not in f"{e.full_name} {c.label} {e.position}".lower():
                    continue
                if status and st != status:
                    continue
                rows.append({"type": "employment", "id": str(c.id), "label": c.label, "party": e.full_name,
                             "subject": e.position, "start_date": c.date.isoformat(),
                             "end_date": e.fired_at.isoformat() if e.fired_at else None, "amount": e.salary,
                             "template": str(c.template_id) if c.template_id else None,
                             "template_name": c.template.name if c.template_id else None, "status": st,
                             "has_snapshot": bool(c.text_snapshot), "branch": str(e.branch_id),
                             "sort": c.date.isoformat()})
        rows.sort(key=lambda r: r["sort"], reverse=True)
        for r in rows:
            r.pop("sort")
        return self.get_paginated_response(self.paginate_queryset(rows))
