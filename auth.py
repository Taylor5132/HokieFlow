from database import supabase


def sign_up(email, password):
    return supabase.auth.sign_up({
        "email": email,
        "password": password,
    })


def create_profile(first_name, last_name, major, graduation_year):
    user_response = supabase.auth.get_user()

    if not user_response or not user_response.user:
        raise ValueError("User is not authenticated.")

    user_id = user_response.user.id

    return (
        supabase.table("profiles")
        .upsert(
            {
                "id": user_id,
                "first_name": first_name,
                "last_name": last_name,
                "major": major,
                "graduation_year": graduation_year,
            },
            on_conflict="id",
        )
        .execute()
    )


def login(email, password):
    return supabase.auth.sign_in_with_password({
        "email": email,
        "password": password,
    })


def get_profile():
    user_response = supabase.auth.get_user()

    if not user_response or not user_response.user:
        return None

    return (
        supabase.table("profiles")
        .select("*")
        .eq("id", user_response.user.id)
        .single()
        .execute()
        .data
    )


def logout():
    return supabase.auth.sign_out()