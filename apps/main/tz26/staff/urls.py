from django.urls import path

from . import salary_views, schedule_views, tasks_api

urlpatterns = [
    path("tasks/", tasks_api.StaffTaskListCreateView.as_view(), name="staff-task-list-create"),
    path("tasks/summary/", tasks_api.StaffTaskSummaryView.as_view(), name="staff-task-summary"),
    path("tasks/<uuid:pk>/", tasks_api.StaffTaskDetailView.as_view(), name="staff-task-detail"),
    path("tasks/<uuid:pk>/files/", tasks_api.StaffTaskFilesView.as_view(), name="staff-task-files"),
    path("tasks/<uuid:pk>/files/<uuid:file_id>/", tasks_api.StaffTaskFilesView.as_view(), name="staff-task-file"),
    path("market-salary/statement/", salary_views.MarketSalaryStatementView.as_view(), name="market-salary-statement"),
    path("staff-schedule/", schedule_views.StaffScheduleListCreateView.as_view(), name="staff-schedule"),
    path("staff-schedule/<uuid:pk>/", schedule_views.StaffScheduleDetailView.as_view(), name="staff-schedule-detail"),
    path("staff/timesheet/", schedule_views.StaffTimesheetView.as_view(), name="staff-timesheet"),
]
