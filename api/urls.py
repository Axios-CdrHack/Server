from django.urls import path, re_path

from . import views

urlpatterns = [
    path("health", views.health),
    path("auth/privy/exchange", views.privy_exchange),
    re_path(r"^cdr-api/(?P<path>.*)$", views.cdr_api_proxy),
    path("profiles", views.profiles_endpoint),
    path("profiles/me", views.my_profile),
    path("users/email-session", views.email_session),
    path("users/wallet", views.wallet_user),
    path("profiles/<str:profile_id>/fields", views.profile_fields),
    path("uploads/avatar", views.upload_avatar),
    path("uploads/field-ip-metadata", views.field_ip_metadata),
    path("fields", views.fields),
    path("verify/start", views.verify_start),
    path("verify/confirm", views.verify_confirm),
    path("cdr/deploy-log", views.cdr_deploy_log),
    path("cdr/server-deploy", views.cdr_server_deploy),
    path("cdr/server-deploy/events", views.cdr_server_deploy_events),
    path("cdr/toggle", views.cdr_toggle),
    path("search/quote", views.search_quote),
    path("search/requests", views.search_requests),
    path("search/requests/<str:quote_id>", views.search_request_detail),
    path("orders", views.orders),
    path("orders/<str:order_id>/export-plan", views.export_plan),
    path("orders/<str:order_id>/export-log", views.export_log),
    path("sales", views.sales),
    path("c/<str:slug>", views.public_card),
]
