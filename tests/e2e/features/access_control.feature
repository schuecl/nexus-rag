Feature: Claims-based access control (FR-18, FR-26, REQUIREMENTS.md Section 6)
  The retrieval access filter and the ingestion tagging constraints are both
  derived entirely server-side from verified OIDC claims. These scenarios pin
  the security invariants that must hold no matter who asks, so a regression
  fails a readable specification rather than an opaque unit test. They run
  in-process (no live stack) on every PR; the same leak properties are also
  checked against the running stack by the golden-query harness
  (scripts/evaluate_retrieval.py's forbidden-document checks).

  Scenario: The retrieval filter only ever returns approved documents
    Given a user "bob-query" with clearance "SECRET" and releasability "FVEY" and org "USAREUR-AF"
    When the server-side access filter is built for that user
    Then the filter requires document status "approved"

  Scenario: A user never retrieves documents above their clearance
    Given a user "bob-query" with clearance "SECRET" and releasability "FVEY" and org "USAREUR-AF"
    And the allowed classifications for that user are "UNCLASSIFIED,CUI,SECRET"
    When the server-side access filter is built for that user
    Then the filter admits only classifications "UNCLASSIFIED,CUI,SECRET"

  Scenario: An unknown clearance admits no classification at all
    Given a user "mallory" with clearance "BOGUS" and releasability "FVEY" and org "USAREUR-AF"
    And no classifications are allowed for that user
    When the server-side access filter is built for that user
    Then the filter admits no classifications

  Scenario: A releasability the user does not hold is never matched
    Given a user "bob-query" with clearance "SECRET" and releasability "FVEY" and org "USAREUR-AF"
    When the server-side access filter is built for that user
    Then the filter admits only releasability "FVEY"
    And the filter does not admit releasability "NOFORN"

  Scenario: Cross-org content is invisible to other orgs
    Given a user "bob-query" with clearance "SECRET" and releasability "FVEY" and org "USAREUR-AF"
    When the server-side access filter is built for that user
    Then the filter admits access scope "USAREUR-AF"
    And the filter admits access scope "ALL_AUTHENTICATED"
    And the filter does not admit access scope "Signal-Corps"

  Scenario: An uploader cannot tag a document above their clearance
    Given a user "alice-ingest" with clearance "CUI" and releasability "FVEY" and org "USAREUR-AF"
    When that user submits metadata with classification "SECRET" and releasability "FVEY"
    Then the submission is rejected with a clearance error

  Scenario: An uploader cannot assign a releasability they do not hold
    Given a user "alice-ingest" with clearance "CUI" and releasability "FVEY" and org "USAREUR-AF"
    When that user submits metadata with classification "CUI" and releasability "NOFORN"
    Then the submission is rejected with a releasability error

  Scenario: A curator's authority is scoped to their own org
    Given a user "carol-curator" with roles "rag-query,rag-curate:USAREUR-AF"
    Then that user can curate org "USAREUR-AF"
    And that user cannot curate org "Signal-Corps"

  Scenario: A supersede target must be approved and within the submitter's authority
    Given a user "alice-ingest" with clearance "CUI" and releasability "FVEY" and org "USAREUR-AF"
    And an existing document with status "pending_review" owned by org "USAREUR-AF"
    When that user names the existing document as a supersede target
    Then the supersede is rejected with a status error

  Scenario: A supersede target in another org is rejected
    Given a user "alice-ingest" with clearance "CUI" and releasability "FVEY" and org "USAREUR-AF"
    And an existing document with status "approved" owned by org "Signal-Corps"
    When that user names the existing document as a supersede target
    Then the supersede is rejected with an org error
