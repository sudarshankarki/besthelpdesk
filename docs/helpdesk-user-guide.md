# BESTSUPPORT Helpdesk User Guide

This guide explains how users create and track support tickets in BESTSUPPORT, including service tickets, CBS access requests, incident tickets, and the approval or resolution workflow for each type.

## 1. Login and Main Pages

Sign in to the helpdesk portal before creating or tracking any request.

Common pages:

- `Dashboard`: Starting point after login.
- `New Ticket`: Create service, access, change, or incident tickets.
- `My Tickets`: View tickets you created, tickets assigned to you, department queue tickets, CBS approvals, and incident reports where you are a signer.
- `Ticket Detail`: View full ticket information, status, attachments, chat, approvals, and report links.

## 2. Ticket Types

When creating a request, choose the request type carefully:

- `Service Request`: Use this for normal support work, such as help with a device, application, printer, email, or routine IT service.
- `Access Request`: Use this for general access that is not CBS access.
- `CBS Access Request (Head Office)`: Use this for Head Office CBS user ID creation or amendment.
- `CBS Access Request (Branch)`: Use this for Branch CBS user ID creation or amendment.
- `Incident`: Use this when something has failed, caused business impact, affected systems, or needs formal incident tracking.
- `Change`: Use this for planned changes.

CBS access request types open a dedicated CBS form instead of the normal ticket form.

## 3. How to Create a Service Ticket

Use this flow for regular helpdesk support requests.

1. Open `Dashboard` or the left sidebar.
2. Click `New Ticket`.
3. Enter a short, clear `Subject`.
4. Set `Request Type` to `Service Request`.
5. Select the responsible `Department`, if known.
6. Select the `Branch`, if applicable.
7. Use `Assign To Email` only when one specific portal user should own the ticket.
8. Use `Notify Email` when a person or group mailbox should receive the email notification.
9. Add `CC Emails` if other users only need to stay informed.
10. Write a clear `Description`.
11. Choose `Impact` and `Urgency`.
12. Attach screenshots or supporting documents if needed.
13. Click `Submit Ticket`.

After submission, the system creates a ticket ID such as `BFC-XXXXXXXXXX`, sets the initial status to `New`, calculates priority from impact and urgency, and sends notification emails.

### Service Ticket Workflow

The usual service ticket flow is:

1. `New`: Ticket has been created.
2. `Acknowledged`: Support has seen or accepted the ticket.
3. `In Progress`: The assigned user or team is working on it.
4. `Waiting on User`: Support needs more information from the requester.
5. `Waiting on Third Party`: Support is waiting for a vendor, external team, or other dependency.
6. `Resolved`: Support has completed the work and provided a resolution note.
7. `Closed`: The ticket is finally closed.

A ticket may also be marked `Cancelled / Duplicate` when it should not continue as a separate request.

## 4. Assignment, Department Queue, and Notifications

The ticket form has three different routing concepts:

- `Assign Department`: Routes the ticket to a department queue.
- `Assign To Email`: Assigns ownership to one specific active portal user.
- `Notify Email`: Sends an email notification, but does not assign ownership.

Important rules:

- Do not use group mailboxes in `Assign To Email`.
- Use group mailboxes such as department emails in `Notify Email`.
- A ticket assigned to a department but not to a person can be claimed by an eligible department user using `Take Ownership`.
- A user cannot assign a new ticket to themself from the create form.

## 5. How to Create a CBS Access Request

Use this flow when a user needs CBS access or an amendment to an existing CBS user ID.

1. Open `New Ticket`.
2. In `Request Type`, choose one of:
   - `CBS Access Request (Head Office)`
   - `CBS Access Request (Branch)`
3. The system opens the dedicated CBS Access Request form.
4. Fill the user information:
   - Name
   - Designation
   - Department or branch/department
   - Employee ID
   - User who needs CBS access
5. Select `Type of User`:
   - `New User`
   - `Amendment for Old User`
6. If this is an amendment, enter the old CBS user ID and reason for amendment.
7. Select all required CBS user groups.
8. Confirm the endorsement checkbox.
9. Select `Assign To After Approval (CBS ACCESS PROVIDER)`, the concerned CBS access provider who should receive the ticket after final approval.
10. Select the digital sign-off chain:
   - Head Office: Recommender and Approver
   - Branch: Recommender, optional Second Recommender, and Approver
11. Attach supporting documents if needed.
12. Optional: click `Download Filled PDF` to preview or keep a filled copy.
13. Click `Submit Request`.

The requester and selected CBS access user must have admin-uploaded profile signatures where required, because the system captures signatures from user profiles.

### CBS Access Workflow

The CBS access workflow is:

1. Requester submits the CBS access form.
2. System creates a CBS access ticket and stores the filled request details.
3. System captures the requester/access-user signature snapshots from user profiles.
4. System stores the selected concerned user / CBS access provider for post-approval assignment.
5. Request goes to the recommender. If no recommender is selected, it goes directly to the approver.
6. For branch requests, after first recommendation it goes to the second recommender only when a second recommender was selected.
7. After recommendation is complete, the request goes to the approver.
8. Approver either approves or rejects the request.

CBS approval statuses:

- `Pending Recommendation`: Waiting for recommender action.
- `Pending Approval`: Waiting for approver action.
- `Approved`: Final approval is complete.
- `Rejected`: Request was rejected and may need correction.

If the request is rejected before final approval, the requester can open the ticket and use `Edit & Resubmit` to correct the CBS form and send it back through the approval chain.

### After CBS Approval

After final CBS approval:

1. The system automatically assigns the approved request to the selected concerned user / CBS access provider.
2. The concerned CBS access provider receives the assignment email with the signed CBS document attached.
3. The system marks the ticket `In Progress` if it was still new or acknowledged.
4. The assigned concerned user completes the CBS work.
5. Support resolves the ticket and includes the CBS user ID or completion message in the resolution note.
6. The ticket can then be closed.

The approved signed document can be downloaded from the ticket detail page.

## 6. How to Create an Incident Ticket

Use an incident ticket when there is a service disruption, security event, system failure, business impact, regulatory concern, or issue that needs formal incident reporting.

1. Open `New Ticket`.
2. Enter a clear `Subject`, such as `CBS login failure affecting multiple users`.
3. Set `Request Type` to `Incident`.
4. Select the responsible `Department`.
5. Select the responsible `Branch`, if applicable.
6. Use `Incident Commander / Ticket Owner Email` to select the user who owns the incident ticket and can manage the draft incident report.
7. Use `Notify Email` and `CC Emails` for awareness.
8. Write the incident `Description`.
9. Select `Impact` and `Urgency`.
10. Fill the incident details section:
    - Detected date/time
    - Current status
    - Additional departments
    - Service affected
    - How it was detected
    - Affected users, systems, or departments
    - Business impact
    - Initial action taken
    - Evidence notes
11. Attach screenshots, logs, or supporting files.
12. Click `Submit Ticket`.

After submission, the incident ticket opens the incident workflow and creates or prepares an Incident Report linked to the ticket.

## 7. Incident Ticket Workflow

The incident ticket has two connected workflows:

- The normal ticket status workflow.
- The formal Incident Report workflow.

### Incident Ticket Status Flow

1. `New`: Incident ticket is created.
2. `Acknowledged`: Support or the incident owner has acknowledged it.
3. `In Progress`: Investigation, containment, recovery, or documentation is ongoing.
4. `Waiting on User` or `Waiting on Third Party`: Used when information or external action is needed.
5. `Resolved`: The incident report has been submitted or the incident work is complete.
6. `Closed`: The incident is fully closed after review.

For incident tickets, resolution may be blocked until the required Incident Report is completed and submitted.

### Incident Report Workflow

1. Open the incident ticket detail page.
2. Click `Create Incident Report` or `Edit Incident Report`.
3. Fill the Incident Report template:
   - Reporting employee information
   - Incident date/time and detection details
   - Source, location/IP, and description
   - Impacted unit, systems, network, and operations
   - Severity: Critical, High, Medium, or Low
   - Recovery actions and verification
   - Quarantine, immediate actions, root cause, eradication, lessons learned, recommendations, and action plan
   - Information sharing details
   - Evidence attachments
4. Select `Incident Registered By`.
5. Choose CC recipients if the final report should be emailed to them.
6. Choose the number of notified sign-off levels, from 1 to 6.
7. Select the users in the sign-off chain.
8. Click `Save` to keep a draft.
9. Required signers open the report and apply their profile signature.
10. After all required signatures are complete, click `Submit & Send`.

When `Submit & Send` succeeds, the system emails the incident response document, marks the report submitted, locks the report for editing, and marks the linked ticket as `Resolved`.

### Incident Sign-Off Rules

Incident signatures use each selected user's admin-uploaded profile signature.

Important rules:

- The selected `Incident Registered By` user signs the registered section.
- Notified sign-off users sign in order.
- Earlier levels are shown as `Reviewed By`.
- The final level is shown as `Acknowledged By`.
- A signer cannot sign before previous sign-off levels are complete.
- If a signer needs changes, they can enter a correction note and click `Request Correction`.

### Incident Correction Workflow

If a reviewer or approver requests correction:

1. The system records the correction note.
2. The report becomes editable again for the incident owner/requester/support user.
3. Signatures from the correction level onward are cleared.
4. If the linked ticket had already been resolved, it moves back to `In Progress`.
5. The incident owner updates the report.
6. The corrected report is saved and signed again in order.
7. Once all signatures are complete, the report can be submitted again.

## 8. Ticket Chat and Audio Call

Open a ticket detail page to use ticket chat.

Users can:

- Send messages related to the ticket.
- Upload chat attachments.
- Download shared files.
- Use the audio call feature when available.

Closed or finalized tickets may make chat read-only.

## 9. Email and Notification Flow

BESTSUPPORT sends email notifications at important points in the ticket workflow. The exact delivery depends on the email addresses saved on the ticket and whether the selected users have email addresses in their profile.

### New Ticket Mail Flow

When a normal service, access, change, or incident ticket is submitted:

1. The requester creates the ticket.
2. If `Notify Email` is filled, the system sends the new-ticket email to that address.
3. If `Notify Email` is blank, the system sends the email to the default support mailbox.
4. If `CC Emails` are filled, those addresses are copied where applicable.
5. If `Assign To Email` is filled, the selected assignee receives an assignment email.
6. The ticket is visible in `My Tickets` for the requester and in the assigned user's queue.
7. If only a department is selected, department users can see it in their department queue and take ownership.

Use this rule:

- `Assign To Email` means ownership.
- `Notify Email` means email information only.
- `CC Emails` means awareness copy only.

### Service Ticket Mail Flow

For service tickets:

1. New-ticket email goes to the notify/default support email.
2. Assignment email goes to the assigned user when a specific user is assigned.
3. Status-change notifications are sent when support updates important statuses.
4. When the ticket is marked `Resolved`, the requester receives the resolution message and any resolution attachments added by support.
5. When the ticket is marked `Closed`, the closure note is saved on the ticket.

### CBS Access Request Mail Flow

For CBS access requests:

1. The requester submits the CBS access form.
2. The system emails the first reviewer:
   - Head Office: Recommender or approver, depending on the selected chain.
   - Branch: First recommender first.
3. When the first recommender approves a branch request, the second recommender receives the next email.
4. When recommendation is complete, the approver receives the approval email.
5. If the request is approved, the requester receives an approval notification.
6. If the request is rejected, the requester receives a rejection notification and can use `Edit & Resubmit`.
7. After final approval, the system automatically assigns the CBS request to the concerned user / CBS access provider selected on the CBS form. That assignee receives an assignment email with the signed document attached.
8. When support resolves the CBS ticket, the requester receives the resolution email. Support should include the CBS User ID or completion detail in the resolution note.

### Incident Mail Flow

For incident tickets and incident reports:

1. The incident ticket creation email follows the normal new-ticket mail flow.
2. When the Incident Report is saved with selected signers, the selected registered/notified users receive sign-off notification emails.
3. Signers open the Incident Report from the email or from `My Tickets`.
4. Signers apply their profile signature in the required order.
5. If a signer requests correction, the incident owner/requester/support user receives a correction notification with the correction note.
6. After correction, updated signers are notified again as needed.
7. When `Submit & Send` is clicked after all signatures are complete, the system emails the final incident response document to selected notified users and CC recipients.
8. The linked incident ticket is marked `Resolved` after successful submission.

### In-App Notifications

In addition to email, users may also receive in-app notifications for:

- New assignment.
- CBS approval or rejection.
- CBS request forwarded to the next reviewer.
- Incident report sign-off request.
- Incident correction request.
- Ticket status updates.
- New ticket chat messages.
- Incoming audio calls.

If an email is not received, users should still check `My Tickets`, because the ticket and approval tasks remain available inside the portal.

## 10. Tracking Your Request

Use `My Tickets` to:

- Search by ticket ID.
- Open ticket detail pages.
- Check status and priority.
- View assigned owner or department queue.
- Continue ticket chat.
- Download CBS or incident documents where available.
- Sign assigned CBS or incident approvals.

## 11. Quick Examples

### Service Request Example

Use `Service Request` when:

- A printer is not working.
- Email is not syncing.
- A workstation needs support.
- A user needs help with a routine IT service.

### CBS Access Example

Use `CBS Access Request (Head Office)` or `CBS Access Request (Branch)` when:

- A new staff member needs CBS access.
- An existing CBS user ID needs amendment.
- CBS user groups need to be added or changed.

### Incident Example

Use `Incident` when:

- CBS, ATM, network, or internet banking is down.
- Multiple users or a department are affected.
- There is a suspected security or regulatory incident.
- A formal incident report and sign-off are required.

## 12. Best Practices

- Write a specific subject.
- Put the full story in the description.
- Select impact and urgency honestly.
- Attach screenshots, logs, or documents when they help.
- Use `Notify Email` for group mailboxes.
- Use `Assign To Email` only for one real portal user.
- For CBS requests, select the correct office type before filling the form.
- For CBS requests, select the correct concerned user / CBS access provider in `Assign To After Approval` before submitting.
- For incidents, assign a clear incident commander/ticket owner.
- Make sure required signers have profile signatures uploaded before approval or incident submission.
