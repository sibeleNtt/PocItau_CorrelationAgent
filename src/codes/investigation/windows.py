from datetime import datetime, timedelta


def matching_reference_window(
    implemented_at: datetime,
    current_start: datetime,
    current_end: datetime,
) -> tuple[datetime, datetime]:
    if current_start >= current_end:
        raise ValueError("A janela atual precisa terminar depois do seu início.")

    reference_date = implemented_at.date() - timedelta(days=1)
    reference_start = current_start.replace(
        year=reference_date.year,
        month=reference_date.month,
        day=reference_date.day,
    )
    return reference_start, reference_start + (current_end - current_start)
