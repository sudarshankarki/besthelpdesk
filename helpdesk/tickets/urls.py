from django.urls import path
from . import views

urlpatterns = [
    path('', views.ticket_list, name='ticket_list'),
    path('new/', views.create_ticket, name='create_ticket'),
    path('docs/', views.tech_docs, name='tech_docs'),
    path('docs/upload/', views.tech_docs_upload, name='tech_docs_upload'),
    path('docs/<int:doc_id>/view/', views.tech_doc_view, name='tech_doc_view'),
    path('docs/<int:doc_id>/download/', views.tech_doc_download, name='tech_doc_download'),
    path('docs/<int:doc_id>/delete/', views.tech_doc_delete, name='tech_doc_delete'),
    path('<int:ticket_id>/', views.ticket_detail, name='ticket_detail'),
    path('<int:ticket_id>/chat/privacy/', views.ticket_chat_privacy_update, name='ticket_chat_privacy_update'),
    path('<int:ticket_id>/chat/seen/', views.ticket_chat_mark_seen, name='ticket_chat_mark_seen'),
    path('<int:ticket_id>/claim/', views.ticket_claim, name='ticket_claim'),
    path('<int:ticket_id>/image/view/', views.ticket_image_view, name='ticket_image_view'),
    path('<int:ticket_id>/image/download/', views.ticket_image_download, name='ticket_image_download'),
    path('<int:ticket_id>/close/<str:token>/', views.ticket_close_via_email, name='ticket_close_via_email'),
    path('<int:ticket_id>/attachments/upload/', views.ticket_attachment_upload, name='ticket_attachment_upload'),
    path('<int:ticket_id>/messages/<int:message_id>/delete/', views.ticket_chat_message_delete, name='ticket_chat_message_delete'),
    path('<int:ticket_id>/attachments/<int:attachment_id>/view/', views.ticket_attachment_view, name='ticket_attachment_view'),
    path('<int:ticket_id>/attachments/<int:attachment_id>/download/', views.ticket_attachment_download, name='ticket_attachment_download'),
    path('support/', views.support_dashboard, name='support_dashboard'),
    path('support/queue/', views.support_queue, name='support_queue'),
    path('support/<int:ticket_id>/update/', views.ticket_update, name='ticket_update'),
]
