"""
Unit tests for src/core/repo_stack_cache.py's RepoStackCache and stack_hash.
"""

from src.core.project_description import ProjectStack
from src.core.repo_stack_cache import RepoStackCache, stack_hash


def _stack_fields(**overrides):
    base = {
        "language": "python",
        "framework": "Django",
        "install_cmd": "pip install -r requirements.txt",
        "dev_cmd": "python manage.py runserver 0.0.0.0:3000",
        "use_hm_reload": False,
        "extra_apt": ["python3", "py3-pip"],
    }
    base.update(overrides)
    return base


class TestStackHash:
    def test_same_key_fields_produce_the_same_hash(self):
        a = ProjectStack(**_stack_fields())
        b = ProjectStack(**_stack_fields())
        assert stack_hash(a) == stack_hash(b)

    def test_different_dev_cmd_produces_a_different_hash(self):
        a = ProjectStack(**_stack_fields())
        b = ProjectStack(**_stack_fields(dev_cmd="flask run --host 0.0.0.0 --port 3000"))
        assert stack_hash(a) != stack_hash(b)

    def test_use_hm_reload_and_extra_apt_do_not_affect_the_hash(self):
        """These fields aren't rendered into the README section, so a
        change to only them shouldn't be treated as "the stack changed"."""
        a = ProjectStack(**_stack_fields(use_hm_reload=False, extra_apt=["a"]))
        b = ProjectStack(**_stack_fields(use_hm_reload=True, extra_apt=["b", "c"]))
        assert stack_hash(a) == stack_hash(b)


class TestRepoStackCacheAiStack:
    def test_get_returns_none_when_nothing_cached(self, tmp_path):
        cache = RepoStackCache(data_dir=tmp_path)
        assert cache.get_ai_stack(7) is None

    def test_store_then_get_round_trips(self, tmp_path):
        cache = RepoStackCache(data_dir=tmp_path)
        fields = _stack_fields()

        cache.store_ai_stack(7, "abc123", fields)
        result = cache.get_ai_stack(7)

        assert result is not None
        fingerprint, stored_fields = result
        assert fingerprint == "abc123"
        assert stored_fields == fields

    def test_persists_across_new_instances(self, tmp_path):
        cache1 = RepoStackCache(data_dir=tmp_path)
        cache1.store_ai_stack(7, "abc123", _stack_fields())

        cache2 = RepoStackCache(data_dir=tmp_path)

        assert cache2.get_ai_stack(7) is not None

    def test_different_projects_are_independent(self, tmp_path):
        cache = RepoStackCache(data_dir=tmp_path)
        cache.store_ai_stack(7, "sha-a", _stack_fields(language="python"))
        cache.store_ai_stack(9, "sha-b", _stack_fields(language="nodejs"))

        assert cache.get_ai_stack(7)[1]["language"] == "python"
        assert cache.get_ai_stack(9)[1]["language"] == "nodejs"

    def test_overwrites_stale_entry_for_same_project(self, tmp_path):
        cache = RepoStackCache(data_dir=tmp_path)
        cache.store_ai_stack(7, "sha-old", _stack_fields(dev_cmd="old"))
        cache.store_ai_stack(7, "sha-new", _stack_fields(dev_cmd="new"))

        fingerprint, fields = cache.get_ai_stack(7)
        assert fingerprint == "sha-new"
        assert fields["dev_cmd"] == "new"


class TestRepoStackCacheReadmeHash:
    def test_get_returns_none_when_nothing_stored(self, tmp_path):
        cache = RepoStackCache(data_dir=tmp_path)
        assert cache.get_readme_hash(7) is None

    def test_store_then_get_round_trips(self, tmp_path):
        cache = RepoStackCache(data_dir=tmp_path)
        cache.store_readme_hash(7, "deadbeef")
        assert cache.get_readme_hash(7) == "deadbeef"

    def test_ai_stack_and_readme_hash_are_independent(self, tmp_path):
        cache = RepoStackCache(data_dir=tmp_path)
        cache.store_ai_stack(7, "sha", _stack_fields())
        cache.store_readme_hash(7, "deadbeef")

        assert cache.get_ai_stack(7) is not None
        assert cache.get_readme_hash(7) == "deadbeef"


class TestRepoStackCacheDiskFailSafety:
    def test_missing_file_yields_empty_cache(self, tmp_path):
        cache = RepoStackCache(data_dir=tmp_path)
        assert cache.get_ai_stack(1) is None
        assert cache.get_readme_hash(1) is None

    def test_corrupt_file_yields_empty_cache_not_a_crash(self, tmp_path):
        path = tmp_path / "repo_stack_cache.json"
        path.write_text("{not valid json")

        cache = RepoStackCache(data_dir=tmp_path)

        assert cache.get_ai_stack(1) is None

    def test_non_dict_json_yields_empty_cache(self, tmp_path):
        path = tmp_path / "repo_stack_cache.json"
        path.write_text("[1, 2, 3]")

        cache = RepoStackCache(data_dir=tmp_path)

        assert cache.get_ai_stack(1) is None

    def test_save_is_atomic_no_leftover_tmp_file_on_success(self, tmp_path):
        cache = RepoStackCache(data_dir=tmp_path)
        cache.store_ai_stack(7, "sha", _stack_fields())

        assert (tmp_path / "repo_stack_cache.json").exists()
        assert not (tmp_path / "repo_stack_cache.json.tmp").exists()
