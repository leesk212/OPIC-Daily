from django.urls import path, include
from api import views as api_views

urlpatterns = [
    path('', api_views.index, name='index'),
    path('api/', include('api.urls')),
]
