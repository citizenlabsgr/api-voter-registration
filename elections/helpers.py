import re
import string
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import bugsnag
import log
import pomace
from bs4 import BeautifulSoup
from nameparser import HumanName

from . import exceptions
from .constants import MI_SOS_URL


###############################################################################
# Shared helpers


def visit(url: str) -> pomace.Page:
    page = pomace.visit(url, browser='chrome', headless=True)
    if page.url != url:
        log.info(f"Revisiting {url} with session cookies")
        page = pomace.visit(url, browser='chrome', headless=True)
    return page


def titleize(text: str) -> str:
    return (
        string.capwords(text)
        .replace(" Of ", " of ")
        .replace(" To ", " to ")
        .replace(" And ", " and ")
        .replace(" In ", " in ")
        .replace(" By ", " by ")
        .replace(" At ", " at ")
        .replace("U.s.", "U.S.")
        .replace("Ii.", "II.")
        .replace("(d", "(D")
        .replace("(l", "(L")
        .replace("(r", "(R")
        .strip()
    )


def normalize_candidate(text: str) -> str:
    if '\n' in text:
        log.debug(f'Handling running mate: {text}')
        text1, text2 = text.split('\n')
        name1 = HumanName(text1.strip())
        name2 = HumanName(text2.strip())
        name1.capitalize()
        name2.capitalize()
        return str(name1) + ' & ' + str(name2)

    name = HumanName(text.strip())
    name.capitalize()
    return str(name)


def normalize_jurisdiction(name: str) -> str:
    name = titleize(name)

    for kind in {'City', 'Township', 'Village'}:
        if name.startswith(kind):
            return name.replace(" Charter", "")

    for kind in {'City', 'Township', 'Village'}:
        if name.endswith(' ' + kind):
            name = kind + ' of ' + name[: -len(kind) - 1]
            return name.replace(" Charter", "")

    return name


def build_mi_sos_url(election_id: int, precinct_id: int) -> str:
    assert election_id, "MI SOS election ID is missing"
    assert precinct_id, "MI SOS precinct ID is missing"
    return f'{MI_SOS_URL}/Voter/GetMvicBallot/{precinct_id}/{election_id}/'


###############################################################################
# Registration helpers


def fetch_registration_status_data(voter):
    url = f'{MI_SOS_URL}/Voter/Index'
    log.info(f"Submitting form on {url}")
    page = visit(url)
    assert "your voter information" in page, f"Invalid voter information: {url}"

    # Submit voter information
    page.fill_first_name(voter.first_name)
    page.fill_last_name(voter.last_name)
    page.select_birth_month(voter.birth_month)
    page.fill_birth_year(voter.birth_year)
    page.fill_zip_code(voter.zip_code)
    page = page.click_search()

    # Parse registration
    registered = None
    for delay in [0, 1]:
        time.sleep(delay)
        if "Yes, you are registered!" in page.text:
            registered = True
            break
        if "No voter record matched your search criteria" in page.text:
            registered = False
            break
        log.warn("Unable to determine registration status")

    # Parse moved status
    recently_moved = "you have recently moved" in page.text
    if recently_moved:
        # TODO: Figure out how to request the new records
        bugsnag.notify(
            exceptions.UnhandledData("Voter has moved"),
            meta_data={"voter": repr(voter), "html": page.text},
        )

    # Parse absentee status
    absentee = "You are on the permanent absentee voter list" in page.text

    # Parse absentee dates
    absentee_dates: Dict[str, Optional[date]] = {
        "Application Received": None,
        "Ballot Sent": None,
        "Ballot Received": None,
    }
    element = page.html.find(id='lblAbsenteeVoterInformation')
    if element:
        strings = list(element.strings) + [""] * 20
        for index, key in enumerate(absentee_dates):
            text = strings[4 + index * 2].strip()
            if text:
                absentee_dates[key] = datetime.strptime(text, '%m/%d/%Y').date()
    else:
        log.warn("Unable to determin absentee status")

    # Parse districts
    districts: Dict = {}
    element = page.html.find(id='lblCountyName')
    if element:
        districts['County'] = _clean_district_name(element.text)
    element = page.html.find(id='lblJurisdName')
    if element:
        districts['Jurisdiction'] = normalize_jurisdiction(element.text)
    element = page.html.find(id='lblWardNumber')
    if element:
        districts['Ward'] = element.text.strip()
    element = page.html.find(id='lblPrecinctNumber')
    if element:
        districts['Precinct'] = element.text.strip()
    # TODO: Parse all districts

    # Parse Polling Location
    polling_location = {
        "PollingLocation": "",
        "PollAddress": "",
        "PollCityStateZip": "",
    }
    for key in polling_location:
        index = page.text.find('lbl' + key)
        if index == -1:
            log.warn("Unable to determine polling location")
            break
        newstring = page.text[(index + len(key) + 5) :]
        end = newstring.find('<')
        polling_location[key] = newstring[0:end]

    return {
        "registered": registered,
        "absentee": absentee,
        "absentee_dates": absentee_dates,
        "districts": districts,
        "polling_location": polling_location,
        "recently_moved": recently_moved,
    }


def _find_or_abort(pattern: str, text: str):
    match = re.search(pattern, text)
    assert match, f"Unable to match {pattern!r} to {text!r}"
    return match[1]


def _clean_district_category(text: str):
    words = text.replace("Judge of ", "").split()
    while words and words[-1] == "District":
        words.pop()
    return " ".join(words)


def _clean_district_name(text: str):
    return text.replace("District District", "District").strip()


###############################################################################
# Ballot helpers


def fetch_ballot(url: str) -> str:
    log.info(f'Fetching ballot: {url}')
    page = visit(url)
    text = page.text.strip()
    assert "Sample Ballot" in text, f"Invalid sample ballot: {url}"
    return text


def parse_election(html: str) -> Tuple[str, Tuple[int, int, int]]:
    """Parse election information from ballot HTML."""
    soup = BeautifulSoup(html, 'html.parser')
    header = soup.find(id='PreviewMvicBallot').div.div.div.text

    election_name_text, election_date_text, *_ = header.strip().split('\n')
    election_name = titleize(election_name_text)
    election_date = datetime.strptime(election_date_text.strip(), '%A, %B %d, %Y')

    return election_name, (election_date.year, election_date.month, election_date.day)


def parse_precinct(html: str, url: str) -> Tuple[str, str, str, str]:
    """Parse precinct information from ballot HTML."""

    # Parse county
    match = re.search(r'(?P<county>[^>]+) County, Michigan', html, re.IGNORECASE)
    assert match, f'Unable to find county name: {url}'
    county = titleize(match.group('county'))

    # Parse jurisdiction
    match = None
    for pattern in [
        r'(?P<jurisdiction>[^>]+), Ward (?P<ward>\d+) Precinct (?P<precinct>\d+)',
        r'(?P<jurisdiction>[^>]+),  Precinct (?P<precinct>\d+[A-Z]?)',
        r'(?P<jurisdiction>[^>]+), Ward (?P<ward>\d+)',
    ]:
        match = re.search(pattern, html)
        if match:
            break
    assert match, f'Unable to find precinct information: {url}'
    jurisdiction = normalize_jurisdiction(match.group('jurisdiction'))

    # Parse ward
    try:
        ward = match.group('ward')
    except IndexError:
        ward = ''

    # Parse number
    try:
        precinct = match.group('precinct')
    except IndexError:
        precinct = ''

    return county, jurisdiction, ward, precinct


def parse_district_from_proposal(category: str, text: str, mi_sos_url: str) -> str:
    patterns = [
        f'[a-z] ((?:[A-Z][A-Za-z.-]+ )+{category})',
        f'\n((?:[A-Z][A-Za-z.-]+ )+{category})',
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, text):
            name = match[1].strip()
            log.debug(f'{pattern!r} matched: {name}')
            if len(name) < 100:
                return name

    raise ValueError(f'Could not find {category!r} in {text!r} on {mi_sos_url}')


def parse_ballot(html: str, data: Dict) -> int:
    """Call all parsers to insert ballot data into the provided dictionary."""
    soup = BeautifulSoup(html, 'html.parser')
    ballot = soup.find(id='PreviewMvicBallot').div.div.find_all('div', recursive=False)[
        1
    ]
    count = 0
    count += parse_primary_election_offices(ballot, data)
    count += parse_general_election_offices(ballot, data)
    count += parse_proposals(ballot, data)
    return count


def parse_primary_election_offices(ballot: BeautifulSoup, data: Dict) -> int:
    """Inserts primary election ballot data into the provided dictionary."""
    count = 0

    offices = ballot.find(id='twoPartyPrimaryElectionOffices')
    if not offices:
        return count

    assert ballot.find(id='primaryColumnHeading1').text.strip() == 'DEMOCRATIC PARTY'
    assert ballot.find(id='primaryColumnHeading2').text.strip() == 'REPUBLICAN PARTY'

    section: Dict = {}
    label = 'primary section'
    data[label] = section

    count += _parse_primary_election_offices("Democratic", ballot, section)
    count += _parse_primary_election_offices("Republican", ballot, section)
    return count


def _parse_primary_election_offices(
    party: str, ballot: BeautifulSoup, data: Dict
) -> int:
    """Inserts primary election ballot data into the provided dictionary."""
    count = 0

    offices = ballot.find(
        id='columnOnePrimary' if party == 'Democratic' else 'columnTwoPrimary'
    )
    if not offices:
        return count

    section: Dict[str, Any] = {}
    division: Optional[List] = None
    data[party] = section

    for index, item in enumerate(
        offices.find_all(
            'div',
            {
                "class": [
                    "division",
                    "office",
                    "term",
                    "candidate",
                    "financeLink",
                    "party",
                ]
            },
        ),
        start=1,
    ):
        log.debug(f'Parsing office element {index}: {item}')

        if "division" in item['class']:
            label = (
                titleize(item.text).replace(" - Continued", "").replace(" District", "")
            )
            try:
                division = section[label]
            except KeyError:
                division = []
            section[label] = division
            office = None

        elif "office" in item['class']:
            label = titleize(item.text)
            assert division is not None, f'Division missing for office: {label}'
            office = {
                'name': label,
                'district': None,
                'type': None,
                'term': None,
                'seats': None,
                'incumbency': None,
                'candidates': [],
            }
            division.append(office)

        elif "term" in item['class']:
            label = item.text
            assert office is not None, f'Office missing for term: {label}'
            if "Incumbent" in label:
                office['type'] = label
            elif "Term" in label:
                office['term'] = label
            elif "Vote for" in label:
                office['seats'] = int(label.replace("Vote for not more than ", ""))
            elif label in {"Incumbent Position", "New Judgeship"}:
                office['incumbency'] = label
            else:
                # TODO: Remove this assert after parsing an entire general election
                assert (
                    "WARD" in label
                    or "DISTRICT" in label
                    or "COURT" in label
                    or "COLLEGE" in label
                    or "Village of " in label
                    or label.endswith(" SCHOOL")
                    or label.endswith(" SCHOOLS")
                    or label.endswith(" ISD")
                    or label.endswith(" ESA")
                    or label.endswith(" COMMUNITY")
                    or label.endswith(" LIBRARY")
                ), f'Unhandled term: {label}'  # pylint: disable=too-many-boolean-expressions
                office['district'] = titleize(label)
            count += 1

        elif "candidate" in item['class']:
            label = normalize_candidate(item.get_text('\n'))
            assert office is not None, f'Office missing for candidate: {label}'
            if label == 'No candidates on ballot':
                continue
            candidate: Dict[str, Any] = {
                'name': label,
                'finance_link': None,
                'party': None,
            }
            office['candidates'].append(candidate)
            count += 1

        elif "financeLink" in item['class']:
            if item.a:
                candidate['finance_link'] = item.a['href']

        elif "party" in item['class']:
            label = titleize(item.text)
            assert candidate is not None, f'Candidate missing for party: {label}'
            candidate['party'] = label or None

    return count


def parse_general_election_offices(ballot: BeautifulSoup, data: Dict) -> int:
    """Inserts general election ballot data into the provided dictionary."""
    count = 0

    offices = ballot.find(id='generalElectionOffices')
    if not offices:
        return count

    section: Optional[Dict] = None
    for index, item in enumerate(
        offices.find_all(
            'div',
            {
                "class": [
                    "section",
                    "division",
                    "office",
                    "term",
                    "candidate",
                    "financeLink",
                    "party",
                ]
            },
        ),
        start=1,
    ):
        log.debug(f'Parsing office element {index}: {item}')

        if "section" in item['class']:
            section = {}
            division: Optional[List] = None
            office: Optional[Dict] = None
            label = item.text.lower()
            if label in data:
                log.warning(f'Duplicate section on ballot: {label}')
                section = data[label]
            else:
                data[label] = section

        elif "division" in item['class']:
            office = None
            label = (
                titleize(item.text).replace(" - Continued", "").replace(" District", "")
            )
            if section is None:
                log.warn(f"Section missing for division: {label}")
                assert list(data.keys()) == ['primary section']
                section = {}
                data['nonpartisan section'] = section
            try:
                division = section[label]
            except KeyError:
                division = []
            section[label] = division
            office = None

        elif "office" in item['class']:
            label = titleize(item.text)
            assert division is not None, f'Division missing for office: {label}'
            office = {
                'name': label,
                'district': None,
                'type': None,
                'term': None,
                'seats': None,
                'incumbency': None,
                'candidates': [],
            }
            division.append(office)

        elif "term" in item['class']:
            label = item.text
            assert office is not None, f'Office missing for term: {label}'
            if "Incumbent" in label:
                office['type'] = label
            elif "Term" in label:
                office['term'] = label
            elif "Vote for" in label:
                office['seats'] = int(label.replace("Vote for not more than ", ""))
            elif label in {"Incumbent Position", "New Judgeship"}:
                office['incumbency'] = label
            else:
                # TODO: Remove this assert after parsing an entire general election
                assert (
                    "WARD" in label
                    or "DISTRICT" in label
                    or "COURT" in label
                    or "COLLEGE" in label
                    or "Village of " in label
                    or label.endswith(" SCHOOL")
                    or label.endswith(" SCHOOLS")
                    or label.endswith(" ISD")
                    or label.endswith(" ESA")
                    or label.endswith(" COMMUNITY")
                    or label.endswith(" LIBRARY")
                ), f'Unhandled term: {label}'  # pylint: disable=too-many-boolean-expressions
                office['district'] = titleize(label)
            count += 1

        elif "candidate" in item['class']:
            label = normalize_candidate(item.get_text('\n'))
            assert office is not None, f'Office missing for candidate: {label}'
            if label == 'No candidates on ballot':
                continue
            candidate = {'name': label, 'finance_link': None, 'party': None}
            office['candidates'].append(candidate)
            count += 1

        elif "financeLink" in item['class']:
            if item.a:
                candidate['finance_link'] = item.a['href']

        elif "party" in item['class']:
            label = titleize(item.text)
            assert candidate is not None, f'Candidate missing for party: {label}'
            candidate['party'] = label or None

    return count


def parse_proposals(ballot: BeautifulSoup, data: Dict) -> int:
    """Inserts proposal data into the provided dictionary."""
    count = 0

    proposals = ballot.find(id='proposals')
    if not proposals:
        return count

    for index, item in enumerate(
        proposals.find_all(
            'div', {"class": ["section", "division", "proposalTitle", "proposalText"]}
        ),
        start=1,
    ):
        log.debug(f'Parsing proposal element {index}: {item}')

        if "section" in item['class']:
            section: Dict[str, Any] = {}
            division: Optional[List] = None
            proposal = None
            label = item.text.lower()
            data[label] = section

        elif "division" in item['class']:
            proposal = None
            label = (
                titleize(item.text).replace(" Proposals", "").replace(" District", "")
            )
            assert label and "Continued" not in label
            try:
                division = section[label]
            except KeyError:
                division = []
            section[label] = division

        elif "proposalTitle" in item['class']:
            label = item.text.strip()
            if label.isupper():
                label = titleize(label)
            if '\n' in label:
                # TODO: Remove duplicate text in description?
                log.warning(f'Newlines in proposal title: {label}')
                if label.count('\n') == 1:
                    label = label.replace('\n', ': ')
            assert division is not None, f'Division missing for proposal: {label}'
            proposal = {'title': label, 'text': None}
            division.append(proposal)

            # Handle proposal text missing a class
            for element in [
                item.parent.next_sibling,
                item.parent.next_sibling.next_sibling,
            ]:
                label = (
                    element.strip()
                    if isinstance(element, str)
                    else element.text.strip()
                )
                if label:
                    log.debug("Parsing proposal text as sibling of proposal title")
                    assert proposal is not None, f'Proposal missing for text: {label}'
                    proposal['text'] = label
                    count += 1
                    break

        elif "proposalText" in item['class']:
            label = item.text.strip()
            assert proposal is not None, f'Proposal missing for text: {label}'
            proposal['text'] = label
            count += 1

    return count
