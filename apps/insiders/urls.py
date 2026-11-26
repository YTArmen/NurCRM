from django.urls import path

from . import views

urlpatterns = [
    path("me/", views.MeView.as_view()),
    path("news/", views.NewsListView.as_view()),
    path("news/<int:pk>/", views.NewsDetailView.as_view()),
    path("sections/", views.SectionsView.as_view()),
    path("topics/", views.TopicListView.as_view()),
    path("topics/<int:pk>/", views.TopicDetailView.as_view()),
    path("topics/<int:pk>/posts/", views.TopicPostView.as_view()),
    path("topics/<int:pk>/metoo/", views.MeTooView.as_view()),
    path("topics/<int:pk>/follow/", views.FollowView.as_view()),
    path("posts/<int:pk>/", views.PostDeleteView.as_view()),
    path("upload/", views.UploadView.as_view()),
]
