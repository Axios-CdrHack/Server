from django.urls import path, re_path

from api import views as common_views
from data import views as data_views
from onchain import views as onchain_views
from users import views as user_views

urlpatterns = [
    path("health", common_views.health),
    path("auth/privy/exchange", user_views.privy_exchange),
    re_path(r"^cdr-api/(?P<path>.*)$", common_views.cdr_api_proxy),
    path("profiles", user_views.profiles_endpoint),
    path("profiles/me", user_views.my_profile),
    path("users/email-session", user_views.email_session),
    path("users/wallet", user_views.wallet_user),
    path("profiles/<str:profile_id>/fields", data_views.profile_fields),
    path("uploads/avatar", user_views.upload_avatar),
    path("uploads/field-ip-metadata", onchain_views.field_ip_metadata),
    path("fields", data_views.fields),
    path("verify/start", data_views.verify_start),
    path("verify/confirm", data_views.verify_confirm),
    path("cdr/deploy-log", onchain_views.cdr_deploy_log),
    path("cdr/server-deploy", onchain_views.cdr_server_deploy),
    path("cdr/server-deploy/events", onchain_views.cdr_server_deploy_events),
    path("cdr/toggle", onchain_views.cdr_toggle),
    path("search/quote", data_views.search_quote),
    path("search/requests", data_views.search_requests),
    path("search/requests/<str:quote_id>/extend", data_views.search_request_extend),
    path("search/requests/<str:quote_id>", data_views.search_request_detail),
    path("orders", data_views.orders),
    path("orders/<str:order_id>/export-plan", data_views.export_plan),
    path("orders/<str:order_id>/export-log", data_views.export_log),
    path("sales", onchain_views.sales),
    path("c/<str:slug>", data_views.public_card),
]
