from handlers.sessions.trial_session import TrialPromptBuilder


def test_my_trial_phrase_routes_to_status_not_new_trial():
    builder = TrialPromptBuilder("dopsy_fs_school")

    assert builder.detect_intent("какие есть у меня пробные занятия") == "my_trial"


def test_generic_trial_word_does_not_start_flow_by_keyword():
    builder = TrialPromptBuilder("dopsy_fs_school")

    assert builder.detect_intent("пробные занятия") is None
