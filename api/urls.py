from django.urls import path
from . import views

urlpatterns = [
    path('health/', views.health, name='health'),
    path('diagnose/', views.diagnose, name='diagnose'),
    path('entries/', views.entries_collection, name='entries'),
    path('entries/<int:entry_id>/', views.entry_detail, name='entry_detail'),
    path('feedback/', views.feedback, name='feedback'),
    path('import/', views.import_data, name='import'),
    path('settings/', views.settings_view, name='settings'),
    path('test-notify/', views.test_notify, name='test_notify'),
]
