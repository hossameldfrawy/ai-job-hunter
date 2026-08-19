"""
Every ingestion source, from A to Z, against fixed markup.

WHY THESE TESTS LOOK LIKE THIS
------------------------------
A scraper fails in two directions, and only one of them is loud:

  LOUD    The site changed its markup and the selector matches nothing. The
          run reports zero postings and someone notices.
  SILENT  The site changed its PARAMETER, or pads a zero-result search with
          unrelated filler, and the scraper happily returns twenty perfectly
          well-formed postings that have nothing to do with the query. Nobody
          notices for months, and the deduplication store fills with noise.

Tanqeeb does exactly the second thing -- `keyword` is ignored in favour of
`keywords`, and an empty search falls back to generic recent listings -- so the
relevance guard is tested as a correctness property, not a nicety.

Every scraper is driven against FIXED markup with the HTTP layer replaced. The
socket block in conftest guarantees that a scraper reaching past the stub
raises rather than quietly fetching the live site.

Run:  python -m pytest tests/test_scrapers_audit.py -v
"""

from __future__ import annotations

import sys
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import http_client                                               # noqa: E402
import scrapers                                                  # noqa: E402
from scrapers.base import (                                      # noqa: E402
    BaseScraper, ScrapeResult, clean, derive_post_title, first_url, parse_date,
    strip_html,
)
from scrapers.facebook import FacebookScraper                    # noqa: E402
from scrapers.job_apis import JobApiScraper                      # noqa: E402
from scrapers.linkedin import LinkedInScraper                    # noqa: E402
from scrapers.rss_feeds import RssScraper                        # noqa: E402
from scrapers.search_proxy import SearchProxyScraper             # noqa: E402
from scrapers.talent import TalentScraper                        # noqa: E402
from scrapers.tanqeeb import TanqeebScraper                      # noqa: E402
from scrapers.telegram_web import TelegramWebScraper             # noqa: E402


@contextmanager
def fake_text(handler):
    """Replace http_client.get_text for the duration of a block."""
    original = http_client.get_text
    http_client.get_text = handler
    try:
        yield
    finally:
        http_client.get_text = original


@contextmanager
def fake_json(handler):
    original = http_client.get_json
    http_client.get_json = handler
    try:
        yield
    finally:
        http_client.get_json = original


# ---------------------------------------------------------------------------
# LinkedIn -- the source that is scraped but NEVER automated
# ---------------------------------------------------------------------------
LINKEDIN_CARDS = """
<ul>
  <li data-entity-urn="urn:li:jobPosting:3912345678">
    <a href="https://www.linkedin.com/jobs/view/voip-engineer-at-etisalat-3912345678?trk=x">x</a>
    <h3 class="base-search-card__title">VoIP Engineer</h3>
    <h4 class="base-search-card__subtitle">Etisalat</h4>
    <span class="job-search-card__location">Dubai, United Arab Emirates</span>
    <time datetime="2026-08-18">1 day ago</time>
  </li>
  <li>
    <a href="https://www.linkedin.com/jobs/view/it-support-4012345678">y</a>
    <h3>IT Support Specialist</h3>
    <h4>Vodafone</h4>
    <span class="job-search-card__location">Cairo, Egypt</span>
  </li>
  <li><span>an advert card with no title</span></li>
</ul>
"""

LINKEDIN_DETAIL = """
<div class="description__text">Administer Asterisk and FreePBX, troubleshoot
SIP trunks, and support the contact centre.</div>
<li class="description__job-criteria-item">Seniority level Entry level</li>
"""


class TestLinkedIn(unittest.TestCase):
    CFG = {
        "queries": [{"keywords": "voip engineer", "locations": ["United Arab Emirates"]}],
        "pages_per_query": 1,
        "enrich_budget": 1,
    }

    def _collect(self, detail=LINKEDIN_DETAIL):
        calls = []

        def handler(url, **kwargs):
            calls.append(url)
            return detail if "jobPosting" in url else LINKEDIN_CARDS

        with fake_text(handler):
            jobs = list(LinkedInScraper(self.CFG, 5, profile={}).collect())
        return jobs, calls

    def test_cards_are_parsed_into_jobposts(self):
        jobs, _ = self._collect()
        titles = {j.title for j in jobs}
        self.assertIn("VoIP Engineer", titles)
        self.assertIn("IT Support Specialist", titles)

    def test_a_card_without_a_title_is_dropped_rather_than_emitted_blank(self):
        jobs, _ = self._collect()
        self.assertTrue(all(j.title for j in jobs))
        self.assertEqual(len(jobs), 2)

    def test_company_location_and_date_survive(self):
        jobs, _ = self._collect()
        job = next(j for j in jobs if j.title == "VoIP Engineer")
        self.assertEqual(job.company, "Etisalat")
        self.assertIn("Dubai", job.location)
        self.assertIsNotNone(job.posted_at)

    def test_tracking_parameters_are_stripped_from_the_url(self):
        """Otherwise the same posting dedups as a new one on every run."""
        jobs, _ = self._collect()
        job = next(j for j in jobs if j.title == "VoIP Engineer")
        self.assertNotIn("trk=", job.url)

    def test_the_job_id_is_extracted_for_enrichment(self):
        jobs, _ = self._collect()
        job = next(j for j in jobs if j.title == "VoIP Engineer")
        self.assertEqual(job.raw["job_id"], "3912345678")

    def test_enrichment_fills_the_description(self):
        jobs, calls = self._collect()
        self.assertTrue(any("jobPosting" in c for c in calls),
                        "the detail endpoint was never called")
        enriched = [j for j in jobs if "Asterisk" in j.description]
        self.assertTrue(enriched, "no posting got a full description")

    def test_the_enrich_budget_is_respected(self):
        """Each detail fetch is a request; an unbounded budget is a rate-limit."""
        _, calls = self._collect()
        self.assertEqual(sum(1 for c in calls if "jobPosting" in c), 1)

    def test_an_empty_response_is_not_a_crash(self):
        with fake_text(lambda url, **kw: ""):
            self.assertEqual(list(LinkedInScraper(self.CFG, 5).collect()), [])

    def test_linkedin_is_permanently_excluded_from_automation(self):
        """The single most important rule in the auto-apply layer.

        LinkedIn is the most productive source here AND the strictest about
        automation, so it is ingest-only. Asserted at the engine's own gate,
        against every spelling a source string can take.
        """
        from auto_apply.engine import is_automatable

        for platform in ("linkedin", "LinkedIn", "LINKEDIN", "linkedin:gcc",
                         "  LinkedIn  ", "linkedin:remote"):
            with self.subTest(platform=platform):
                allowed, reason = is_automatable(platform)
                self.assertFalse(allowed, f"{platform!r} would be automated")
                self.assertIn("manual-only", reason)

    def test_other_boards_remain_automatable(self):
        from auto_apply.engine import is_automatable

        for platform in ("tanqeeb:uae", "talent:ae", "wuzzuf", "rss:weworkremotely"):
            with self.subTest(platform=platform):
                self.assertTrue(is_automatable(platform)[0])


# ---------------------------------------------------------------------------
# Tanqeeb -- Arabic-first, and the source that pads empty searches
# ---------------------------------------------------------------------------
TANQEEB_RELEVANT = """
<div class="search-job-card">
  <a href="/jobs/021136159.html"><h2>اخصائي دعم فني - IT Support</h2></a>
  <div class="company-name">Erada Egypt</div>
  <div class="job-location">On-site - Cairo</div>
</div>
<div class="search-job-card">
  <a href="/jobs/021136160.html"><h2>Governess for a private family</h2></a>
  <div class="company-name">Al Nahda</div>
  <div class="job-location">Riyadh</div>
</div>
"""

TANQEEB_LD = """
<script type="application/ld+json">
{"@type":"JobPosting","description":"<p>Support Issabel PBX and SIP trunks.</p>",
 "datePosted":"2026-08-17","hiringOrganization":{"name":"Erada Egypt Ltd"}}
</script>
"""


class TestTanqeeb(unittest.TestCase):
    CFG = {"countries": ["egypt"], "terms": ["دعم فني"], "enrich_budget": 0}

    def test_the_query_parameter_is_the_plural_keywords(self):
        """`keyword`, `q` and `search` are silently IGNORED by Tanqeeb.

        The page still returns HTTP 200 with twenty cards -- just not the ones
        you asked for. That failure is invisible without this assertion.
        """
        seen = {}

        def handler(url, **kwargs):
            seen["url"] = url
            return TANQEEB_RELEVANT

        with fake_text(handler):
            list(TanqeebScraper(self.CFG, 5).collect())
        self.assertIn("keywords=", seen["url"])
        self.assertNotIn("keyword=", seen["url"].replace("keywords=", ""))

    def test_filler_from_a_zero_result_search_is_rejected(self):
        """A zero-result search returns generic recent listings, not nothing."""
        with fake_text(lambda url, **kw: TANQEEB_RELEVANT):
            jobs = list(TanqeebScraper(self.CFG, 5).collect())
        titles = [j.title for j in jobs]
        self.assertTrue(any("دعم فني" in t for t in titles))
        self.assertFalse(any("Governess" in t for t in titles),
                         "unrelated filler was injected into the pipeline")

    def test_the_country_is_always_present_in_the_location(self):
        """The location scorer needs it; the card only prints the city."""
        with fake_text(lambda url, **kw: TANQEEB_RELEVANT):
            jobs = list(TanqeebScraper(self.CFG, 5).collect())
        self.assertIn("Egypt", jobs[0].location)

    def test_relative_urls_are_made_absolute(self):
        with fake_text(lambda url, **kw: TANQEEB_RELEVANT):
            jobs = list(TanqeebScraper(self.CFG, 5).collect())
        self.assertTrue(jobs[0].url.startswith("https://egypt.tanqeeb.com/"))

    def test_query_tokens_drop_generic_words(self):
        self.assertNotIn("it", TanqeebScraper._query_tokens("it support"))
        self.assertIn("support", TanqeebScraper._query_tokens("it support"))

    def test_a_short_query_still_yields_a_token(self):
        self.assertTrue(TanqeebScraper._query_tokens("voip"))

    def test_enrichment_reads_the_jsonld_jobposting(self):
        cfg = dict(self.CFG, enrich_budget=2)

        def handler(url, **kwargs):
            return TANQEEB_LD if "021136159" in url else TANQEEB_RELEVANT

        with fake_text(handler):
            jobs = list(TanqeebScraper(cfg, 5).collect())
        job = jobs[0]
        self.assertIn("Issabel", job.description)
        self.assertIsNotNone(job.posted_at)
        self.assertIn("Erada", job.company)

    def test_jsonld_extraction_ignores_other_schema_blocks(self):
        html = ('<script type="application/ld+json">{"@type":"WebSite"}</script>'
                + TANQEEB_LD)
        node = TanqeebScraper._jobposting_ld(html)
        self.assertEqual(node["@type"], "JobPosting")

    def test_broken_jsonld_returns_none_rather_than_raising(self):
        self.assertIsNone(TanqeebScraper._jobposting_ld(
            '<script type="application/ld+json">{not json</script>'
        ))

    def test_no_terms_configured_means_no_requests(self):
        calls = []
        with fake_text(lambda url, **kw: calls.append(url) or ""):
            self.assertEqual(list(TanqeebScraper({"countries": ["egypt"]}, 5)
                                  .collect()), [])
        self.assertEqual(calls, [])

    def test_a_failing_country_does_not_stop_the_others(self):
        cfg = {"countries": ["egypt", "saudi"], "terms": ["voip"],
               "enrich_budget": 0}

        def handler(url, **kwargs):
            if "egypt" in url:
                raise RuntimeError("HTTP 503")
            return TANQEEB_RELEVANT.replace("دعم فني", "voip")

        with fake_text(handler):
            jobs = list(TanqeebScraper(cfg, 5).collect())
        self.assertTrue(jobs, "one dead subdomain killed the whole source")


# ---------------------------------------------------------------------------
# talent.com
# ---------------------------------------------------------------------------
TALENT_CARDS = """
<div class="JobCard_card__a1b2">
  <a href="/view?id=99"><span class="JobCard_title__X32Qk">IT Support Engineer</span></a>
  <span class="JobCard_company__Zz9">Ninja</span>
  <span class="JobCard_location__Q1">Riyadh</span>
  <span class="JobCard_snippet__M2">Troubleshoot desktops and networks.</span>
  <time datetime="2026-08-18">yesterday</time>
</div>
<div class="JobCard_card__a1b2">
  <span class="JobCard_title__X32Qk"></span>
</div>
"""


class TestTalent(unittest.TestCase):
    CFG = {"countries": ["ae"], "terms": ["it support"], "max_per_query": 10}

    def test_hashed_css_module_classes_are_matched_by_prefix(self):
        """talent.com re-hashes its class names on every front-end deploy.

        Matching `JobCard_title__X32Qk` verbatim would break within weeks and
        look exactly like a quiet zero-result day.
        """
        with fake_text(lambda url, **kw: TALENT_CARDS):
            jobs = list(TalentScraper(self.CFG, 5).collect())
        self.assertEqual([j.title for j in jobs], ["IT Support Engineer"])

    def test_metadata_and_absolute_urls(self):
        with fake_text(lambda url, **kw: TALENT_CARDS):
            job = list(TalentScraper(self.CFG, 5).collect())[0]
        self.assertEqual(job.company, "Ninja")
        self.assertEqual(job.location, "Riyadh")
        self.assertIn("Troubleshoot", job.description)
        self.assertTrue(job.url.startswith("https://ae.talent.com/"))
        self.assertEqual(job.source, "talent:ae")

    def test_a_missing_location_falls_back_to_the_country(self):
        html = TALENT_CARDS.replace(
            '<span class="JobCard_location__Q1">Riyadh</span>', ""
        )
        with fake_text(lambda url, **kw: html):
            job = list(TalentScraper(self.CFG, 5).collect())[0]
        self.assertEqual(job.location, "United Arab Emirates")

    def test_max_per_query_caps_the_batch(self):
        cfg = dict(self.CFG, max_per_query=0)
        with fake_text(lambda url, **kw: TALENT_CARDS):
            self.assertEqual(list(TalentScraper(cfg, 5).collect()), [])

    def test_every_country_dead_is_logged_as_a_markup_break(self):
        with self.assertLogs("scrapers.talent", level="ERROR") as captured:
            with fake_text(lambda url, **kw: "<html></html>"):
                list(TalentScraper(self.CFG, 5).collect())
        self.assertIn("changed its markup", "\n".join(captured.output))

    def test_no_terms_means_no_work(self):
        self.assertEqual(list(TalentScraper({"countries": ["ae"]}, 5).collect()),
                         [])


# ---------------------------------------------------------------------------
# Wuzzuf / Bayt via the search proxy (they 403 a datacentre IP directly)
# ---------------------------------------------------------------------------
GOOGLE_NEWS_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>IT Support Specialist at Fawry - Wuzzuf</title>
    <link>https://news.google.com/rss/articles/CBM123</link>
    <description>&lt;p&gt;Cairo based support role.&lt;/p&gt;</description>
    <pubDate>Mon, 18 Aug 2026 09:00:00 GMT</pubDate>
  </item>
  <item>
    <title>774 odoo-erp Jobs in Egypt - Wuzzuf</title>
    <link>https://news.google.com/rss/articles/CBM456</link>
    <description>listing page</description>
  </item>
  <item>
    <title>Network Engineer Salary in Egypt - Wuzzuf</title>
    <link>https://news.google.com/rss/articles/CBM789</link>
    <description>salary guide</description>
  </item>
</channel></rss>
"""


class TestSearchProxy(unittest.TestCase):
    CFG = {
        "engines": ["google_news"],
        "site_queries": [{"site": "wuzzuf.net", "terms": ["it support"]}],
        "max_items_per_query": 25,
    }

    def test_the_site_operator_is_sent(self):
        seen = {}

        def handler(url, **kwargs):
            seen["url"] = url
            return GOOGLE_NEWS_RSS

        with fake_text(handler):
            list(SearchProxyScraper(self.CFG, 5).collect())
        self.assertIn("site%3Awuzzuf.net", seen["url"])

    def test_listing_pages_and_salary_guides_are_filtered_out(self):
        """The proxy indexes whole boards, not just vacancies."""
        with fake_text(lambda url, **kw: GOOGLE_NEWS_RSS):
            jobs = list(SearchProxyScraper(self.CFG, 5).collect())
        titles = [j.title for j in jobs]
        self.assertEqual(len(titles), 1, titles)
        self.assertIn("IT Support Specialist", titles[0])

    def test_the_publisher_suffix_is_stripped_from_the_title(self):
        with fake_text(lambda url, **kw: GOOGLE_NEWS_RSS):
            job = list(SearchProxyScraper(self.CFG, 5).collect())[0]
        self.assertNotIn("- Wuzzuf", job.title)

    def test_the_source_names_the_board_it_came_from(self):
        with fake_text(lambda url, **kw: GOOGLE_NEWS_RSS):
            job = list(SearchProxyScraper(self.CFG, 5).collect())[0]
        self.assertEqual(job.source, "search:wuzzuf.net")
        self.assertEqual(job.raw["via"], "google_news_rss")

    def test_disabling_google_news_disables_the_source(self):
        """Bing ignores `site:` and DuckDuckGo answers a bot challenge, so
        there is no second engine to fall back to."""
        cfg = dict(self.CFG, engines=["bing"])
        calls = []
        with fake_text(lambda url, **kw: calls.append(url) or ""):
            self.assertEqual(list(SearchProxyScraper(cfg, 5).collect()), [])
        self.assertEqual(calls, [])

    def test_one_failing_term_does_not_lose_the_rest(self):
        cfg = {"engines": ["google_news"], "site_queries": [
            {"site": "bayt.com", "terms": ["voip", "it support"]}]}

        def handler(url, **kwargs):
            if "voip" in url:
                raise RuntimeError("HTTP 429")
            return GOOGLE_NEWS_RSS

        with fake_text(handler):
            self.assertTrue(list(SearchProxyScraper(cfg, 5).collect()))


# ---------------------------------------------------------------------------
# Generic RSS
# ---------------------------------------------------------------------------
FEED_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Remote Systems Ltd: VoIP Support Engineer</title>
    <link>https://weworkremotely.com/jobs/1</link>
    <description>&lt;p&gt;Asterisk &amp;amp; SIP.&lt;/p&gt;</description>
    <pubDate>Sun, 17 Aug 2026 12:00:00 GMT</pubDate>
  </item>
  <item>
    <title></title><link>https://example.com/empty</link>
  </item>
</channel></rss>
"""


class TestRssFeeds(unittest.TestCase):
    CFG = {"feeds": [{"name": "weworkremotely",
                      "url": "https://weworkremotely.com/rss"}]}

    def test_entries_become_jobposts(self):
        with fake_text(lambda url, **kw: FEED_XML):
            jobs = list(RssScraper(self.CFG, 5).collect())
        self.assertEqual(len(jobs), 1, "an untitled entry was emitted")
        self.assertEqual(jobs[0].source, "rss:weworkremotely")

    def test_html_is_stripped_and_entities_decoded(self):
        with fake_text(lambda url, **kw: FEED_XML):
            job = list(RssScraper(self.CFG, 5).collect())[0]
        self.assertNotIn("<p>", job.description)
        self.assertIn("Asterisk & SIP", job.description)

    def test_the_company_is_recovered_from_a_colon_title(self):
        """WeWorkRemotely encodes "Company: Role" and has no author field."""
        with fake_text(lambda url, **kw: FEED_XML):
            job = list(RssScraper(self.CFG, 5).collect())[0]
        self.assertEqual(job.company, "Remote Systems Ltd")

    def test_an_unreachable_feed_is_skipped_not_fatal(self):
        with fake_text(lambda url, **kw: ""):
            self.assertEqual(list(RssScraper(self.CFG, 5).collect()), [])

    def test_unparseable_xml_is_skipped(self):
        with fake_text(lambda url, **kw: "<<<not xml at all"):
            self.assertEqual(list(RssScraper(self.CFG, 5).collect()), [])

    def test_a_feed_with_no_url_is_ignored(self):
        with fake_text(lambda url, **kw: FEED_XML):
            self.assertEqual(
                list(RssScraper({"feeds": [{"name": "x"}]}, 5).collect()), []
            )


# ---------------------------------------------------------------------------
# Keyless job APIs
# ---------------------------------------------------------------------------
class TestJobApis(unittest.TestCase):
    PAYLOADS = {
        "arbeitnow": {"data": [{"title": "VoIP Engineer",
                                "company_name": "Acme", "location": "Remote",
                                "url": "https://a/1", "description": "<b>SIP</b>",
                                "created_at": 1755500000}]},
        "remoteok": [{"legal": "notice"},
                     {"position": "Telephony Engineer", "company": "RemoteCo",
                      "url": "https://r/1", "description": "Asterisk",
                      "epoch": 1755500000}],
        "jobicy": {"jobs": [{"jobTitle": "IT Support", "companyName": "Jobicy",
                             "jobGeo": "Anywhere", "url": "https://j/1",
                             "jobExcerpt": "Support"}]},
        "himalayas": {"jobs": [{"title": "NOC Engineer",
                                "companyName": "Him", "companySlug": "him",
                                "locationRestrictions": ["EMEA"],
                                "excerpt": "NOC"}]},
        "remotive": {"jobs": [{"title": "Sysadmin", "company_name": "Rem",
                               "candidate_required_location": "Worldwide",
                               "url": "https://re/1", "description": "Linux"}]},
        "themuse": {"results": [{"name": "Support Analyst",
                                 "company": {"name": "Muse"},
                                 "locations": [{"name": "Cairo"}],
                                 "refs": {"landing_page": "https://m/1"},
                                 "contents": "<p>Analyst</p>"}]},
    }

    def _run(self, enabled, payload_for):
        with fake_json(payload_for):
            return list(JobApiScraper({"sources": enabled}, 5).collect())

    def test_every_adapter_maps_its_vendor_schema(self):
        for name, payload in self.PAYLOADS.items():
            with self.subTest(api=name):
                jobs = self._run({name: True}, lambda url, **kw: payload)
                self.assertTrue(jobs, f"{name} produced nothing")
                self.assertTrue(jobs[0].title)
                self.assertTrue(jobs[0].source.startswith("api:"))

    def test_disabled_apis_are_never_requested(self):
        calls = []

        def handler(url, **kwargs):
            calls.append(url)
            return {}

        self._run({"arbeitnow": False}, handler)
        self.assertEqual(calls, [])

    def test_remoteok_legal_notice_row_is_not_a_job(self):
        jobs = self._run({"remoteok": True},
                         lambda url, **kw: self.PAYLOADS["remoteok"])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Telephony Engineer")

    def test_an_unreachable_api_is_skipped_not_fatal(self):
        self.assertEqual(self._run({"arbeitnow": True},
                                   lambda url, **kw: None), [])

    def test_a_changed_schema_is_skipped_with_a_warning(self):
        """The whole point of the JSON sources is that they do not break the
        run when a vendor renames a field."""
        with self.assertLogs("scrapers.job_apis", level="WARNING"):
            jobs = self._run({"themuse": True},
                             lambda url, **kw: {"results": [{"company": "oops"}]})
        self.assertEqual(jobs, [])

    def test_html_in_descriptions_is_stripped(self):
        jobs = self._run({"arbeitnow": True},
                         lambda url, **kw: self.PAYLOADS["arbeitnow"])
        self.assertNotIn("<b>", jobs[0].description)


# ---------------------------------------------------------------------------
# Telegram public web preview
# ---------------------------------------------------------------------------
TME_HTML = """
<div class="tgme_widget_message_wrap">
  <div class="tgme_widget_message" data-post="jobsgulf/4521">
    <div class="tgme_widget_message_text">🔥 مطلوب مهندس دعم فني VoIP للعمل في دبي.
    خبرة في Asterisk و SIP. للتقديم:
    <a href="https://careers.example.com/apply/99">Apply here</a></div>
    <time datetime="2026-08-18T08:00:00+00:00"></time>
  </div>
</div>
<div class="tgme_widget_message_wrap">
  <div class="tgme_widget_message" data-post="jobsgulf/4522">
    <div class="tgme_widget_message_text">Good morning everyone, hope the week
    is going well for all of you here in the group.</div>
  </div>
</div>
"""


class TestTelegramWeb(unittest.TestCase):
    CFG = {"channels": ["jobsgulf"], "messages_per_channel": 40}

    def test_a_vacancy_post_becomes_a_jobpost(self):
        with fake_text(lambda url, **kw: TME_HTML):
            jobs = list(TelegramWebScraper(self.CFG, 5).collect())
        self.assertEqual(len(jobs), 1, "channel chatter was ingested as a job")
        self.assertIn("VoIP", jobs[0].description)
        self.assertEqual(jobs[0].source, "telegram:jobsgulf")

    def test_a_real_apply_link_beats_the_telegram_permalink(self):
        with fake_text(lambda url, **kw: TME_HTML):
            job = list(TelegramWebScraper(self.CFG, 5).collect())[0]
        self.assertIn("careers.example.com", job.url)
        self.assertIn("t.me/jobsgulf/4521", job.raw["permalink"])

    def test_the_timestamp_is_read_from_the_post(self):
        with fake_text(lambda url, **kw: TME_HTML):
            job = list(TelegramWebScraper(self.CFG, 5).collect())[0]
        self.assertIsNotNone(job.posted_at)

    def test_paging_stops_instead_of_looping_on_repeated_pages(self):
        """`?before=` walks backwards; a channel that keeps returning the same
        page must terminate, not spin."""
        calls = []

        def handler(url, **kwargs):
            calls.append(url)
            if len(calls) > 12:
                raise AssertionError("the pager did not terminate")
            return TME_HTML

        with fake_text(handler):
            list(TelegramWebScraper(self.CFG, 5).collect())
        self.assertLessEqual(len(calls), 12)

    def test_no_channels_configured_is_warned_about(self):
        with self.assertLogs("scrapers.telegram_web", level="WARNING"):
            self.assertEqual(list(TelegramWebScraper({}, 5).collect()), [])

    def test_it_needs_no_credentials_at_all(self):
        """This is the property that lets it run in a stateless cloud runner."""
        seen = {}

        def handler(url, **kwargs):
            seen["headers"] = kwargs.get("headers") or {}
            return TME_HTML

        with fake_text(handler):
            list(TelegramWebScraper(self.CFG, 5).collect())
        self.assertNotIn("Cookie", seen["headers"])
        self.assertNotIn("Authorization", seen["headers"])

    def test_the_channel_slug_is_normalised(self):
        cfg = {"channels": ["@jobsgulf"]}
        self.assertEqual(TelegramWebScraper(cfg, 5).channels, ["jobsgulf"])

    def test_an_unreachable_channel_is_survivable(self):
        with fake_text(lambda url, **kw: ""):
            self.assertEqual(list(TelegramWebScraper(self.CFG, 5).collect()), [])


# ---------------------------------------------------------------------------
# Facebook -- honest about what it cannot do
# ---------------------------------------------------------------------------
FB_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>مطلوب مهندس شبكات - Facebook</title>
    <link>https://news.google.com/rss/articles/FB1</link>
    <description>وظيفة شاغرة</description>
  </item>
</channel></rss>
"""


class TestFacebook(unittest.TestCase):
    CFG = {"search_terms": ["مطلوب مهندس شبكات"], "pages": []}

    def test_indexed_mode_needs_no_cookie(self):
        with fake_text(lambda url, **kw: FB_RSS):
            jobs = list(FacebookScraper(self.CFG, 5).collect())
        self.assertTrue(jobs)
        self.assertEqual(jobs[0].source, "facebook:indexed")

    def test_the_facebook_suffix_is_stripped(self):
        with fake_text(lambda url, **kw: FB_RSS):
            job = list(FacebookScraper(self.CFG, 5).collect())[0]
        self.assertNotIn("Facebook", job.title)

    def test_without_a_cookie_the_authenticated_path_is_not_attempted(self):
        cfg = {"search_terms": [], "pages": ["somepage"]}
        calls = []
        with fake_text(lambda url, **kw: calls.append(url) or ""):
            list(FacebookScraper(cfg, 5).collect())
        self.assertFalse(any("mbasic" in c for c in calls),
                         "mbasic was fetched with no credential to use")

    def test_indexed_only_mode_says_so(self):
        with self.assertLogs("scrapers.facebook", level="INFO") as captured:
            with fake_text(lambda url, **kw: FB_RSS):
                list(FacebookScraper(self.CFG, 5).collect())
        self.assertIn("indexed-only", "\n".join(captured.output))


# ---------------------------------------------------------------------------
# The registry and the error boundary around every source
# ---------------------------------------------------------------------------
class TestRegistryAndBoundary(unittest.TestCase):
    def test_every_config_key_maps_to_a_scraper(self):
        for key in ("linkedin", "telegram", "telegram_user", "talent",
                    "tanqeeb", "job_apis", "search_proxy", "rss", "facebook"):
            with self.subTest(key=key):
                self.assertIn(key, scrapers.REGISTRY)

    def test_every_registered_scraper_declares_a_stable_name(self):
        for key, cls in scrapers.REGISTRY.items():
            with self.subTest(key=key):
                self.assertTrue(cls.name)
                self.assertNotEqual(cls.name, "base")

    def test_a_scraper_that_raises_cannot_take_down_the_run(self):
        class Exploding(BaseScraper):
            name = "exploding"

            def collect(self):
                raise RuntimeError("the site is on fire")

        result = Exploding({}).run()
        self.assertIsInstance(result, ScrapeResult)
        self.assertFalse(result.ok)
        self.assertIn("on fire", result.error)
        self.assertEqual(result.jobs, [])

    def test_run_all_reports_every_source_even_when_some_fail(self):
        class Good(BaseScraper):
            name = "good"

            def collect(self):
                from models import JobPost

                return [JobPost(source="good", title="VoIP Engineer")]

        class Bad(BaseScraper):
            name = "bad"

            def collect(self):
                raise RuntimeError("nope")

        jobs, results = scrapers.run_all([Good({}), Bad({})], max_workers=2)
        self.assertEqual(len(jobs), 1)
        self.assertEqual({r.name for r in results}, {"good", "bad"})
        self.assertEqual([r.ok for r in sorted(results, key=lambda r: r.name)],
                         [False, True])

    def test_empty_postings_are_dropped_by_the_boundary(self):
        class Blank(BaseScraper):
            name = "blank"

            def collect(self):
                from models import JobPost

                return [JobPost(source="blank", title="", description="")]

        self.assertEqual(Blank({}).run().count, 0)

    def test_run_all_with_no_scrapers_is_a_clean_pass(self):
        self.assertEqual(scrapers.run_all([]), ([], []))


# ---------------------------------------------------------------------------
class TestSharedParsingHelpers(unittest.TestCase):
    """Every scraper leans on these; a regression here is a regression in all."""

    def test_strip_html_and_entities(self):
        self.assertEqual(strip_html("<p>a &amp; b</p>"), "a & b")
        self.assertEqual(strip_html(None), "")

    def test_clean_collapses_whitespace_and_caps(self):
        self.assertEqual(clean("  a\n\n b  "), "a b")
        self.assertEqual(len(clean("x" * 100, 10)), 10)

    def test_parse_date_across_every_format_our_sources_emit(self):
        for value in ("2026-08-18", "2026-08-18T08:00:00+00:00",
                      "Mon, 18 Aug 2026 09:00:00 GMT", 1755500000,
                      1755500000000, "18 Aug 2026", "3 days ago",
                      "2 hours ago"):
            with self.subTest(value=value):
                self.assertIsNotNone(parse_date(value))

    def test_parse_date_returns_none_for_junk(self):
        for value in ("", None, 0, "sometime next week", "not a date"):
            with self.subTest(value=value):
                self.assertIsNone(parse_date(value))

    def test_first_url_trims_trailing_punctuation(self):
        self.assertEqual(first_url("apply at https://x.com/a. thanks"),
                         "https://x.com/a")
        self.assertEqual(first_url("no links here"), "")

    def test_title_derivation_skips_decoration_and_contact_lines(self):
        text = ("🔥🔥🔥\n"
                "مطلوب\n"
                "اخصائي دعم فني VoIP\n"
                "للتواصل واتساب 01000000000")
        self.assertEqual(derive_post_title(text), "اخصائي دعم فني VoIP")

    def test_title_never_comes_back_empty(self):
        for text in ("", "   ", "🔥", "مطلوب"):
            with self.subTest(text=text):
                self.assertTrue(derive_post_title(text))


if __name__ == "__main__":
    unittest.main(verbosity=2)
