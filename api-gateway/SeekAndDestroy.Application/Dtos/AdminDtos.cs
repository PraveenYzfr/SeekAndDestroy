namespace SeekAndDestroy.Application.Dtos;

/// <summary>Points one model role at one provider/model. The role itself is
/// a path segment rather than a field here, matching the AI service's
/// PUT /api/admin/model-roles/{role_name}.
///
/// Deliberately NOT validated against a role enum in the gateway. The list of
/// roles belongs to the code that calls the models (app.agents.roles), and a
/// copy here would be a second place to update every time a chain is added -
/// which is exactly how answer_rejection_question ended up with no role at
/// all. The AI service validates against the live list and returns 400.</summary>
public sealed record ModelRoleAssignmentDto(string Provider, string Model);
