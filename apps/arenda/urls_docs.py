from django.urls import path

from . import views_docs as v

urlpatterns = [
    path("templates/", v.TemplateListView.as_view()),
    path("templates/fields/", v.TemplateFieldsView.as_view()),
    path("templates/import/", v.TemplateImportView.as_view()),
    path("templates/<uuid:pk>/", v.TemplateDetailView.as_view()),
    path("templates/<uuid:pk>/copy/", v.TemplateCopyView.as_view()),
    path("templates/<uuid:pk>/archive/", v.TemplateArchiveView.as_view()),
    path("leases/preview/", v.LeasePreviewView.as_view()),
    path("leases/sign/", v.LeaseSignView.as_view()),
    path("leases/<uuid:pk>/document/", v.LeaseDocumentView.as_view()),
    path("leases/<uuid:pk>/document.pdf", v.LeaseDocumentView.as_view(), {"fmt": "pdf"}),
    path("leases/<uuid:pk>/document.docx", v.LeaseDocumentView.as_view(), {"fmt": "docx"}),
    path("contracts/", v.ContractsBaseView.as_view()),
    path("employees/<uuid:pk>/contracts/preview/", v.EmployeeContractPreviewView.as_view()),
    path("employees/<uuid:pk>/contracts/sign/", v.EmployeeContractSignView.as_view()),
    path("employment-contracts/", v.EmploymentContractListView.as_view()),
    path("employment-contracts/<uuid:pk>/document/", v.EmploymentContractDocumentView.as_view()),
    path("employment-contracts/<uuid:pk>/document.pdf", v.EmploymentContractDocumentView.as_view(), {"fmt": "pdf"}),
    path("employment-contracts/<uuid:pk>/document.docx", v.EmploymentContractDocumentView.as_view(), {"fmt": "docx"}),
]
