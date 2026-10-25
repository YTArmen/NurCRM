from django.urls import path
from rest_framework.routers import SimpleRouter

from . import views_team as v

router = SimpleRouter()
router.register(r"expenses", v.ExpenseViewSet, basename="arenda-expense")
router.register(r"expense-categories", v.ExpenseCategoryViewSet, basename="arenda-expense-category")
router.register(r"tax-payments", v.TaxPaymentViewSet, basename="arenda-tax-payment")
router.register(r"assets", v.AssetViewSet, basename="arenda-asset")
router.register(r"employees", v.EmployeeViewSet, basename="arenda-employee")

urlpatterns = [
    path("finance/summary/", v.FinanceSummaryView.as_view()),
    path("month-summary/", v.FinanceSummaryView.as_view()),
    path("payroll/", v.PayrollView.as_view()),
    path("payroll/<str:month>/advance/", v.PayrollAdvanceView.as_view()),
    path("payroll/<str:month>/salary/", v.PayrollSalaryView.as_view()),
] + router.urls
