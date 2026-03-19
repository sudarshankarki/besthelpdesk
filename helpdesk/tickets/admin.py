from django.contrib import admin
from django.http import HttpResponse
from django.urls import path
from django.utils import timezone
import csv
from io import StringIO
from datetime import timedelta

from .models import GroupMailboxEmail, TechnicalDocument, Ticket, TicketAssignmentLog, TicketMessage


@admin.register(GroupMailboxEmail)
class GroupMailboxEmailAdmin(admin.ModelAdmin):
    list_display = ("email", "department", "created_at")
    list_filter = ("department",)
    search_fields = ("email", "department__name")
    ordering = ("email",)


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = (
        "ticket_id",
        "subject",
        "request_type",
        "department",
        "notify_email",
        "created_by",
        "assigned_to",
        "priority",
        "status",
        "created_at",
        "resolved_at",
        "closed_at",
        "time_to_resolve",
    )
    list_filter = ("status", "priority", "request_type", "department", "created_at")
    search_fields = (
        "ticket_id",
        "subject",
        "description",
        "created_by__username",
        "assigned_to__username",
    )
    readonly_fields = (
        "ticket_id",
        "created_at",
        "updated_at",
        "resolved_at",
        "closed_at",
        "time_to_resolve",
    )
    actions = ("export_tickets_csv",)
    change_list_template = "admin/tickets/ticket/change_list.html"

    def time_to_resolve(self, obj):
        return obj.formatted_ttr()

    time_to_resolve.short_description = "Time to Resolve"

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "report/",
                self.admin_site.admin_view(self.report_view),
                name="tickets_ticket_report",
            ),
        ]
        return custom_urls + urls

    def _get_filtered_queryset(self, request):
        cl = self.get_changelist_instance(request)
        return cl.get_queryset(request)

    def _format_duration(self, duration):
        if not duration:
            return ""
        total_minutes = int(duration.total_seconds() // 60)
        hours, minutes = divmod(total_minutes, 60)
        days, hours = divmod(hours, 24)
        if days:
            return f"{days}d {hours}h {minutes}m"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    def _tickets_csv_response(self, tickets, filename):
        output = StringIO()
        writer = csv.writer(output)

        solved_statuses = {"resolved", "closed", "cancelled_duplicate"}
        total = tickets.count()
        solved = tickets.filter(status__in=solved_statuses).count()
        unsolved = total - solved

        durations = []
        for ticket in tickets:
            end_time = ticket.closed_at or ticket.resolved_at
            if end_time:
                durations.append(end_time - ticket.created_at)

        avg_seconds = int(sum(d.total_seconds() for d in durations) / len(durations)) if durations else 0
        avg_duration = timedelta(seconds=avg_seconds) if avg_seconds else None

        writer.writerow(["Summary"])
        writer.writerow(["Total tickets", total])
        writer.writerow(["Solved (resolved/closed/cancelled)", solved])
        writer.writerow(["Unsolved (active)", unsolved])
        writer.writerow(["Average time to solve", self._format_duration(avg_duration)])
        writer.writerow([])

        writer.writerow(
            [
                "Ticket ID",
                "Subject",
                "Department",
                "Status",
                "Priority",
                "Created At",
                "Solved At",
                "Time To Solve",
                "Created By",
                "Assigned To (Solver)",
            ]
        )
        for ticket in tickets:
            created_at = timezone.localtime(ticket.created_at) if ticket.created_at else None
            solved_at = ticket.closed_at or ticket.resolved_at
            solved_at_local = timezone.localtime(solved_at) if solved_at else None
            duration = (solved_at - ticket.created_at) if solved_at else None
            writer.writerow(
                [
                    ticket.ticket_id or "",
                    ticket.subject or "",
                    ticket.department or "",
                    ticket.status,
                    ticket.priority,
                    created_at.strftime("%Y-%m-%d %H:%M") if created_at else "",
                    solved_at_local.strftime("%Y-%m-%d %H:%M") if solved_at_local else "",
                    self._format_duration(duration),
                    getattr(ticket.created_by, "email", "") or getattr(ticket.created_by, "username", ""),
                    getattr(ticket.assigned_to, "email", "") or getattr(ticket.assigned_to, "username", ""),
                ]
            )

        response = HttpResponse(output.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    def report_view(self, request):
        qs = (
            self._get_filtered_queryset(request)
            .select_related("created_by", "assigned_to")
            .order_by("-created_at")
        )
        now = timezone.localtime(timezone.now()).strftime("%Y%m%d-%H%M")
        return self._tickets_csv_response(qs, filename=f"tickets-report-{now}.csv")

    @admin.action(description="Export selected tickets (CSV)")
    def export_tickets_csv(self, request, queryset):
        qs = queryset.select_related("created_by", "assigned_to").order_by("-created_at")
        now = timezone.localtime(timezone.now()).strftime("%Y%m%d-%H%M")
        return self._tickets_csv_response(qs, filename=f"tickets-selected-{now}.csv")


@admin.register(TicketMessage)
class TicketMessageAdmin(admin.ModelAdmin):
    list_display = ("ticket", "author", "created_at")
    search_fields = ("ticket__ticket_id", "ticket__subject", "author__username", "body")
    list_filter = ("created_at",)


@admin.register(TicketAssignmentLog)
class TicketAssignmentLogAdmin(admin.ModelAdmin):
    list_display = ("ticket", "assigned_to", "assigned_by", "assigned_at", "unassigned_at")
    list_filter = ("assigned_at", "unassigned_at")
    search_fields = (
        "ticket__ticket_id",
        "ticket__subject",
        "assigned_to__username",
        "assigned_to__email",
        "assigned_by__username",
        "assigned_by__email",
    )


@admin.register(TechnicalDocument)
class TechnicalDocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "visibility", "filename", "uploaded_by", "created_at")
    list_filter = ("visibility", "created_at")
    search_fields = ("title", "filename", "uploaded_by__username", "uploaded_by__email")
    readonly_fields = ("object_key", "filename", "content_type", "size", "uploaded_by", "created_at")
    actions = None
    filter_horizontal = ("allowed_users",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.has_perm("tickets.delete_technicaldocument")
