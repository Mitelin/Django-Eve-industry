from django.urls import path

from apps.industry_planner.views import (
    calculate_blueprint_by_id,
    calculate_blueprints,
    create_project,
    get_project,
    list_projects,
    ore_material,
    rebuild_project,
    shadow_planner_report,
    update_project,
)


urlpatterns = [
    path("blueprints/calculate", calculate_blueprints, name="blueprints-calculate"),
    path("blueprints/<int:type_id>/calculate", calculate_blueprint_by_id, name="blueprint-by-id-calculate"),
    path("ore/material", ore_material, name="ore-material"),
    path("reports/shadow/planner", shadow_planner_report, name="reports-shadow-planner"),
    path("planner/projects", list_projects, name="planner-projects-list"),
    path("planner/projects/create", create_project, name="planner-project-create"),
    path("planner/projects/<int:project_id>", get_project, name="planner-project-detail"),
    path("planner/projects/<int:project_id>/update", update_project, name="planner-project-update"),
    path("planner/projects/<int:project_id>/rebuild", rebuild_project, name="planner-project-rebuild"),
]