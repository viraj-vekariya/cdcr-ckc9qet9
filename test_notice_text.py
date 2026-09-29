from notice_text import detail_body, valid_layout, normalize

def test_full_notice_header_removed_and_url_separated():
    source = 'Type : PLACEMENT\nSubject : CV Submission\nCompany : TREXQUANT\n\nForm Link: https://forms.gle/8HC94Fg4iyMTr28f7\n\nInterested students apply.'
    assert detail_body(source) == 'Form Link: https://forms.gle/8HC94Fg4iyMTr28f7\n\nInterested students apply.'

def test_gemini_cannot_edit_deadlines_or_urls():
    source = 'Apply by 29 September. https://example.com/a?x=23CH10019&y=2'
    assert valid_layout(source, source.replace('. ', '.\n\n'))
    assert not valid_layout(source, source.replace('29', '30'))
    assert not valid_layout(source, source.replace('23CH', '23\nCH'))
    assert not valid_layout(source, source+' Apply now')

def test_unchanged_content_without_metadata():
    assert detail_body('Company news\n\nSome text') == 'Company news\n\nSome text'

def test_normalization_preserves_query_and_unicode():
    source = 'https://example.org/?a=1&b=2\n\n₹91,60,000\r\nDeadline: 16:00'
    assert normalize(source) == source.replace('\r\n','\n')
