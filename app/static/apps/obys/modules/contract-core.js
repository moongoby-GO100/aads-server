/* 오비서 계약서 공용 코어 — 표준 조항 기본값·작성 검증·미리보기 HTML.
 *
 * 아래 상수·함수 본문은 기존 관리화면(index.html)의 계약서 작성 코드에서 그대로
 * 옮겨 왔다(2026-10-06, 원본 index.html 의 contractTypeLabels~standardContractDefaults,
 * validateContractDraft, contractClause, contractPreviewHtml). 현재 관리자 화면(V4.1)이
 * 같은 양식·같은 검증으로 계약서를 만들게 하기 위함이다. 서버(_validate_contract_payload)가
 * 최종 판정이고, 이 검증은 저장 전에 빠진 칸을 먼저 알려주는 용도다.
 * 양식을 바꿀 때는 index.html 쪽도 함께 바꿔라 — 두 화면의 계약서가 달라지면 안 된다.
 * (tests/unit/test_obys_v41_contract_editor.py 가 두 사본의 일치를 검사한다.)
 */
(function (root) {
    "use strict";
    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
    }
    function formatMoney(value) {
      return `${Math.round(Number(value) || 0).toLocaleString("ko-KR")}원`;
    }
    // 계약일은 KST 날짜다 — toISOString() 은 UTC 라 자정~09시에 하루 전 날짜가 된다.
    function today() {
      return new Date(Date.now() + 9 * 3600 * 1000).toISOString().slice(0, 10);
    }
    function contractValue(contract, camelKey, snakeKey = "") {
      return contract?.[camelKey] ?? (snakeKey ? contract?.[snakeKey] : undefined) ?? "";
    }
    function previewText(value, fallback = "-") {
      const text = String(value ?? "").trim();
      return escapeHtml(text || fallback).replace(/\n/g, "<br>");
    }
    // 도장 이미지는 저장된 business_id 로만 고른다. 지점명으로 사업자를 추정하지 않는다.
    function businessForBranchName() { return {}; }

    const contractTypeLabels = {
      part_time: "단시간/아르바이트 근로계약서",
      regular: "정규직 근로계약서",
      manager: "매니저 근로계약서",
      freelancer: "3.3% 프리랜서 용역계약서",
      confidentiality: "보안/개인정보 서약서"
    };
    const employmentTaxTypeLabels = {
      four_insurance: "4대보험 가입 근로자",
      freelancer_33: "3.3% 프리랜서 원천징수"
    };
    const wageTypeLabels = {
      hourly: "시급",
      monthly: "월급",
      daily: "일급",
      case_fee: "건별 용역비"
    };
    const contractStatusLabels = {
      draft: ["작성중", "info"],
      requested: ["서명요청", "warn"],
      signed: ["서명완료", "good"],
      archived: ["보관", "info"],
      cancelled: ["취소", "bad"]
    };
    const MINIMUM_HOURLY_WAGE_2026 = 10320;

    const standardContractDefaults = {
      common: {
        workplace: "해당 지점 영업장 및 사용자가 지정한 관련 업무 장소",
        jobDescription: "홀 응대, 주방보조, 포장/배달 주문 확인, 매장 청소, 위생관리, 마감정산 보조 등 매장 운영에 필요한 업무",
        workTime: "근로계약서 저장 전 실제 시작·종료시각을 확정 입력",
        restTime: "근로시간 4시간 이상 30분, 8시간 이상 60분 이상을 근로자가 자유롭게 이용",
        weeklyHours: "주 소정근로시간은 실제 근무표 기준으로 확정하고 법정 한도 내에서 운영",
        workDays: "주 5일 또는 매장 근무표에 따른 협의 근무",
        holidays: "주휴일은 근무표에 따라 지정하고 주휴수당은 법정 요건 충족 시 지급",
        payDate: "매월 5일",
        payMethod: "직원 본인 명의 계좌이체",
        probationPeriod: "수습기간 해당 없음. 필요 시 입사 후 3개월 이내로 별도 명시",
        wageComposition: "기본급, 주휴수당, 연장·야간·휴일근로수당, 식대 또는 기타 수당, 법정 공제항목을 급여내역서에 구분 기재",
        overtimeTerms: "연장·야간·휴일근로는 사전 승인 및 근태기록 기준으로 운영하고, 적용 대상 법정수당을 급여마감에 반영",
        leaveTerms: "연차유급휴가, 지각, 조퇴, 결근, 병가, 무단결근은 근태기록과 법정 기준에 따라 급여내역서에 반영",
        mealUniformTerms: "식사 제공 여부, 유니폼 지급·반납, 보건증 제출·갱신, 위생교육 이수 기준을 준수",
        terminationTerms: "퇴직 시 원활한 인수인계를 위해 사전 통보하도록 노력한다. 해고·계약해지는 법령상 제한, 해고예고, 소명 기회, 서면 통지 기준을 준수하며 위약금 또는 손해배상 예정액을 미리 정하지 않는다.",
        confidentialityTerms: "고객정보, 직원정보, 매출·정산자료, 레시피, 배달앱·포스 계정정보, 위생수칙을 준수하고 무단 열람·반출·공유를 금지",
        terms: "본 계약서에 명시되지 않은 사항은 근로기준법, 최저임금법, 개인정보보호법 등 관계 법령과 사업장 취업규칙 또는 내부 운영기준에 따른다. 임금, 소정근로시간, 휴일, 연차유급휴가, 임금 구성항목·계산방법·지급방법은 서면 또는 전자문서로 명시하고 서명 완료본을 근로자에게 교부한다. PDF와 인쇄본은 A4 세로 문서 기준으로 보관한다."
      },
      part_time: {
        weeklyHours: "주 소정근로시간과 근로일별 근로시간은 확정 근무표에 명시",
        workDays: "월/화/수/목/금/토/일 중 협의된 요일",
        dailyWorkSchedule: "근로일별 시작·종료·휴게시간을 확정하여 입력",
        terms: "단시간근로자는 근로일별 시작·종료·휴게시간을 서면에 확정 기재한다. 본 계약서에 명시되지 않은 사항은 근로기준법, 최저임금법 등 관계 법령에 따른다."
      },
      regular: {
        workplaceSizeCategory: "under_5",
        wageType: "monthly",
        wage: 3000000,
        mealProvision: "employer_meal",
        baseSalary: 3000000,
        nonTaxMealAllowance: 0,
        taxableAllowance: 0,
        workTime: "21:00-09:00 (휴게 제외 실제 근로 10시간 30분)",
        restTime: "01:00-02:00, 05:30-06:00 (총 1시간 30분, 근로자가 자유롭게 이용)",
        weeklyHours: "주 52시간 30분 (1일 10시간 30분 × 주 5일)",
        workDays: "주 5일, 구체적 근무요일은 월별 근무표로 사전 지정",
        holidays: "1주 소정근로일 개근 및 주 15시간 이상 근무 시 주 1회 유급 주휴일. 근로자의 날은 유급휴일",
        probationPeriod: "입사 후 3개월. 근태, 직무수행, 위생, 고객응대, 보안 준수 여부를 객관적 기록으로 평가하며 월 3,000,000원 급여는 수습 중에도 동일 적용",
        wageComposition: "월 총액 3,000,000원. 사용자 식사 제공 시 과세 기본급 3,000,000원. 식사를 제공하지 않고 현금 식대를 지급하는 경우에만 과세 기본급 2,800,000원 + 비과세 식대 200,000원으로 구분하며 급여명세서에 표시",
        overtimeTerms: "상시 5인 미만 상태에서는 근로기준법 제56조의 50% 가산 규정이 원칙적으로 적용되지 않으나, 약정 근로시간을 초과한 실제 근로시간의 통상임금은 별도 지급한다. 상시근로자 수가 5인 이상이 되거나 별도 합의가 있으면 법정 가산수당을 적용한다.",
        leaveTerms: "상시 5인 미만 상태에서는 근로기준법 제60조 연차유급휴가가 원칙적으로 적용되지 않는다. 다만 경조·질병 등 사유의 휴가는 매장 운영에 지장이 없는 범위에서 내부 승인으로 부여할 수 있고, 상시 5인 이상 전환 시 법정 연차 기준을 즉시 적용한다.",
        insuranceTerms: "정규직 근로소득으로 신고하고 국민연금·건강보험·고용보험·산재보험의 취득·보수 신고 및 근로소득 원천징수를 각 법정 기준에 따라 처리한다. 3.3% 사업소득으로 신고하지 않는다.",
        terminationTerms: "근로자는 원활한 인수인계를 위해 퇴직 희망일 30일 전 서면 통보하도록 노력한다. 사용자는 근로기준법 제26조의 해고예고 또는 해고예고수당 기준을 준수한다. 무단결근, 위생·보안 중대 위반은 사실 확인과 소명 기회를 거쳐 조치하며 위약금·손해배상액을 미리 정하지 않는다.",
        terms: "기간의 정함이 없는 근로계약으로 한다. 휴게시간에는 업무 지시를 하지 않으며 근로자가 자유롭게 이용한다. 근무표 변경은 사전에 통지하고, 상시근로자 수 변동으로 강행법규의 적용 범위가 달라지면 변경된 법정 기준을 적용한다."
      },
      manager: {
        jobDescription: "매장 운영관리, 포스 마감, 현금·정산 점검, 위생·근태 관리, 직원 교육, 배달앱 운영계정 관리, 고객 클레임 대응",
        wageType: "monthly",
        weeklyHours: "주 40시간 기준. 매장 운영상 필요한 마감·정산 업무는 관리자 승인 기준으로 처리",
        terms: ""
      },
      freelancer: {
        wageType: "case_fee",
        jobDescription: "독립 용역 업무",
        workTime: "근무시간을 정하지 않고 산출물 납품 일정과 검수 기준으로 관리",
        restTime: "근로시간 지휘·감독 대상 아님",
        weeklyHours: "해당 없음",
        workDays: "해당 없음",
        holidays: "해당 없음",
        freelancerScope: "고용노동부 노무제공자 공통 표준계약서 기준에 맞춰 계약 목적, 구체적 업무범위, 산출물 형식, 납품일, 검수 기준, 재작업 범위, 수급인의 독립 수행 방식을 개별 계약서에 명확히 기재",
        freelancerSettlementTerms: "용역비는 산출물 검수 완료 후 합의된 지급일에 지급하며, 사업소득 원천징수 3.3% 적용 여부, 비용 부담 주체, 자료 제공 범위, 지연 지급 기준, 중도해지 시 기성 정산 기준을 명시한다.",
        terminationTerms: "계약 위반, 산출물 미제출, 보안 위반, 협업 불능 등 객관적 사유가 있는 경우 서면 통지 후 해지한다. 이미 검수 완료된 산출물의 정산 권리와 반환·삭제해야 할 자료 범위는 별도로 보호한다.",
        confidentialityTerms: "영업비밀, 고객정보, 계정정보, 매출·정산자료, 레시피를 용역 목적 외로 사용하거나 제3자에게 제공하지 않는다.",
        terms: "수급인은 독립 사업자로서 근로시간·근무장소·업무수행방법에 대한 직접 지휘감독을 받지 않는다. 실제 운영이 고정 근무, 상시 지휘감독, 대체인력 제한, 매장 상시업무 편입 등 근로자성에 해당하면 근로계약 전환 여부를 즉시 검토한다. 계약서와 검수·정산 기록은 A4 세로 문서 기준으로 보관한다."
      },
      confidentiality: {
        jobDescription: "영업비밀, 개인정보, 매출·정산자료, 계정정보 보호 서약",
        terms: "보안/개인정보 서약서는 근로계약 또는 용역계약과 별도로 체결하며, 퇴사·계약 종료 후에도 비밀유지 의무를 적용한다."
      },
      four_insurance: {
        insuranceTerms: "4대보험 취득 신고 대상 여부를 확인하고 국민연금, 건강보험, 고용보험, 산재보험 및 근로소득 원천징수를 법정 기준에 따라 처리"
      },
      freelancer_33: {
        insuranceTerms: "사업소득 3.3% 원천징수 기준으로 신고하며, 근로자성이 인정될 수 있는 지휘감독·고정근로 운영은 하지 않는다."
      }
    };
    function validateContractDraft(draft) {
      const missing = [];
      const requireValue = (label, value) => {
        if (!String(value ?? "").trim()) missing.push(label);
      };
      requireValue("승인 직원", draft.employeeRequestId);
      requireValue("직원명", draft.employeeName);
      requireValue("직원 이메일", draft.employeeEmail);
      requireValue("근로자 연락처", draft.employeePhone);
      requireValue("근로자 생년월일", draft.employeeBirthDate);
      requireValue("근로자 주소", draft.employeeAddress);
      requireValue("사업자/지점", draft.businessId && draft.branch);
      requireValue("계약 작성일", draft.contractDate);
      requireValue("사용자 상호", draft.employerName);
      requireValue("사업자등록번호", draft.employerRegistrationNo);
      requireValue("대표자", draft.employerRepresentative);
      requireValue("사용자 주소", draft.employerAddress);
      if (draft.employeeBirthDate) {
        const birthDate = new Date(`${draft.employeeBirthDate}T00:00:00`);
        const referenceDate = new Date(`${draft.startDate || draft.contractDate || today()}T00:00:00`);
        if (Number.isNaN(birthDate.getTime()) || birthDate > referenceDate) throw new Error("근로자 생년월일을 확인하십시오.");
        let age = referenceDate.getFullYear() - birthDate.getFullYear();
        const beforeBirthday = referenceDate.getMonth() < birthDate.getMonth()
          || (referenceDate.getMonth() === birthDate.getMonth() && referenceDate.getDate() < birthDate.getDate());
        if (beforeBirthday) age -= 1;
        if (age < 18) {
          requireValue("친권자/후견인 성명", draft.minorGuardianName);
          requireValue("친권자/후견인 연락처", draft.minorGuardianPhone);
          if (draft.minorGuardianConsent !== "confirmed") missing.push("친권자/후견인 동의서 확인");
        }
      }
      if (draft.startDate && draft.endDate && draft.endDate < draft.startDate) {
        throw new Error("계약 종료일은 입사일보다 빠를 수 없습니다.");
      }
      if (["part_time", "regular", "manager"].includes(draft.contractType)) {
        if (draft.employmentTaxType !== "four_insurance") throw new Error("근로계약서는 4대보험 가입 근로자 구분으로 작성해야 합니다.");
        [
          ["입사일", draft.startDate], ["근무장소", draft.workplace], ["업무내용", draft.jobDescription],
          ["근무시간", draft.workTime], ["휴게시간", draft.restTime], ["주 소정근로시간", draft.weeklyHours],
          ["근무일/요일", draft.workDays], ["휴일/주휴", draft.holidays], ["급여지급일", draft.payDate],
          ["지급방법", draft.payMethod], ["임금 구성/공제", draft.wageComposition],
          ["연장·야간·휴일근로", draft.overtimeTerms], ["연차/휴가/결근", draft.leaveTerms],
          ["4대보험/세무 처리", draft.insuranceTerms]
        ].forEach(([label, value]) => requireValue(label, value));
        if (draft.contractType === "part_time") requireValue("근로일별 근로시간", draft.dailyWorkSchedule);
        if (draft.foreignWorker) {
          requireValue("국적", draft.employeeNationality);
          requireValue("체류자격", draft.visaStatus);
          requireValue("외국인등록번호(마스킹)", draft.foreignRegistrationNoMasked);
        }
        if (!draft.wage || draft.wage <= 0) missing.push("확정 임금");
        const components = draft.baseSalary + draft.nonTaxMealAllowance + draft.taxableAllowance;
        if (components && components !== draft.wage) throw new Error("기본급·비과세 식대·기타 과세수당 합계가 월 총액과 일치해야 합니다.");
        if (draft.nonTaxMealAllowance > 200000) throw new Error("비과세 식대는 월 200,000원을 초과할 수 없습니다.");
        if (draft.mealProvision !== "cash_no_meal" && draft.nonTaxMealAllowance > 0) throw new Error("사용자가 식사를 제공하는 경우 현금 식대를 비과세로 분류할 수 없습니다.");
        const weeklyMatch = String(draft.weeklyHours || "").match(/주\s*(\d+)시간(?:\s*(\d+)분)?/);
        if (draft.contractType === "regular" && draft.workplaceSizeCategory === "under_5" && weeklyMatch && draft.baseSalary > 0) {
          const weeklyHours = Number(weeklyMatch[1]) + Number(weeklyMatch[2] || 0) / 60;
          const monthlyPaidHours = (weeklyHours + Math.min(8, weeklyHours / 5)) * 365 / 7 / 12;
          const conservativeHourly = draft.baseSalary / monthlyPaidHours;
          if (conservativeHourly < MINIMUM_HOURLY_WAGE_2026) throw new Error(`과세 기본급 기준 환산시급 ${formatMoney(Math.floor(conservativeHourly))}은 2026년 최저임금 ${formatMoney(MINIMUM_HOURLY_WAGE_2026)}보다 낮습니다.`);
        }
      } else if (draft.contractType === "freelancer") {
        if (draft.employmentTaxType !== "freelancer_33" || draft.wageType !== "case_fee") {
          throw new Error("프리랜서 용역계약서는 3.3% 원천징수·건별/용역비 방식으로 작성해야 합니다.");
        }
        const fixedWorkSignals = [draft.workTime, draft.weeklyHours, draft.workDays].join(" ");
        if (/(출퇴근|근무표|주\s*\d+\s*시간|월\/화|화\/수|수\/목|목\/금|금\/토|토\/일|상시|교대)/.test(fixedWorkSignals)) {
          throw new Error("프리랜서 용역계약서에 고정 근무표·출퇴근·상시 교대 근무 표현이 있습니다. 해당 운영이면 근로계약서로 작성하십시오.");
        }
        requireValue("용역 시작일", draft.startDate);
        requireValue("용역 수행장소/방식", draft.workplace);
        requireValue("용역 업무내용", draft.jobDescription);
        requireValue("용역 업무범위/산출물", draft.freelancerScope);
        requireValue("용역비 정산/해지", draft.freelancerSettlementTerms);
        requireValue("계약 해지/기성정산 기준", draft.terminationTerms);
        requireValue("비밀유지/자료보호", draft.confidentialityTerms);
        if (!draft.wage || draft.wage <= 0) missing.push("확정 용역비");
      }
      if (missing.length) throw new Error(`필수 입력값을 확인하십시오: ${[...new Set(missing)].join(", ")}`);
    }

    function contractClause(title, body) {
      return `<li><b>${escapeHtml(title)}</b><br>${previewText(body, "미입력")}</li>`;
    }

    function contractPreviewHtml(contract) {
      const contractType = contractValue(contract, "contractType", "contract_type");
      const employmentTaxType = contractValue(contract, "employmentTaxType", "employment_tax_type");
      const foreignWorker = Boolean(contractValue(contract, "foreignWorker", "foreign_worker"));
      const title = contractTypeLabels[contractType] || contractValue(contract, "contract_label") || "근로계약서";
      const isFreelancerContract = contractType === "freelancer";
      const isConfidentialityContract = contractType === "confidentiality";
      const partyLabel = isFreelancerContract ? "수급인" : isConfidentialityContract ? "서약자" : "근로자";
      const signatureLabel = isFreelancerContract ? "수급인 서명" : isConfidentialityContract ? "서약자 서명" : "근로자 서명";
      const contractDetailHeading = isFreelancerContract ? "용역 상세 조항" : isConfidentialityContract ? "서약 상세 조항" : "계약 상세 조항";
      const employeeName = contractValue(contract, "employeeName", "employee_name");
      const employerName = contractValue(contract, "employerName", "employer_name");
      const startDate = contractValue(contract, "startDate", "start_date");
      const endDate = contractValue(contract, "endDate", "end_date");
      const contractDate = contractValue(contract, "contractDate", "contract_date");
      const wage = Number(contractValue(contract, "wage") || 0);
      const wageType = contractValue(contract, "wageType", "wage_type") || "hourly";
      const taxLabel = employmentTaxTypeLabels[employmentTaxType] || contractValue(contract, "employment_tax_label") || "-";
      const workplace = contractValue(contract, "workplace", "workplace") || contractValue(contract, "branch");
      const jobDescription = contractValue(contract, "jobDescription", "job_description");
      const weeklyHours = contractValue(contract, "weeklyHours", "weekly_hours");
      const workTime = contractValue(contract, "workTime", "work_time");
      const restTime = contractValue(contract, "restTime", "rest_time");
      const workDays = contractValue(contract, "workDays", "work_days");
      const dailyWorkSchedule = contractValue(contract, "dailyWorkSchedule", "daily_work_schedule");
      const holidays = contractValue(contract, "holidays", "holidays");
      const payDate = contractValue(contract, "payDate", "pay_date");
      const payMethod = contractValue(contract, "payMethod", "pay_method") || "직원 명의 계좌이체";
      const wageComposition = contractValue(contract, "wageComposition", "wage_composition");
      const workplaceSizeCategory = contractValue(contract, "workplaceSizeCategory", "workplace_size_category") || "under_5";
      const mealProvision = contractValue(contract, "mealProvision", "meal_provision") || "employer_meal";
      const baseSalary = Number(contractValue(contract, "baseSalary", "base_salary") || 0);
      const nonTaxMealAllowance = Number(contractValue(contract, "nonTaxMealAllowance", "non_tax_meal_allowance") || 0);
      const taxableAllowance = Number(contractValue(contract, "taxableAllowance", "taxable_allowance") || 0);
      const overtimeTerms = contractValue(contract, "overtimeTerms", "overtime_terms");
      const leaveTerms = contractValue(contract, "leaveTerms", "leave_terms");
      const insuranceTerms = contractValue(contract, "insuranceTerms", "insurance_terms");
      const mealUniformTerms = contractValue(contract, "mealUniformTerms", "meal_uniform_terms");
      const freelancerScope = contractValue(contract, "freelancerScope", "freelancer_scope");
      const freelancerSettlementTerms = contractValue(contract, "freelancerSettlementTerms", "freelancer_settlement_terms");
      const terminationTerms = contractValue(contract, "terminationTerms", "termination_terms");
      const confidentialityTerms = contractValue(contract, "confidentialityTerms", "confidentiality_terms");
      const signatureDataUri = contractValue(contract, "signatureDataUri", "signature_data_uri");
      const signedAt = contractValue(contract, "signedAt", "signed_at");
      const signerName = contractValue(contract, "signerName", "signer_name") || employeeName;
      const employeeBirthDate = contractValue(contract, "employeeBirthDate", "employee_birth_date");
      const employeePhone = contractValue(contract, "employeePhone", "employee_phone");
      const employeeNationality = contractValue(contract, "employeeNationality", "employee_nationality") || "대한민국";
      const bankName = contractValue(contract, "bankName", "bank_name");
      const bankAccountHolder = contractValue(contract, "bankAccountHolder", "bank_account_holder");
      const bankAccountMasked = contractValue(contract, "bankAccountMasked", "bank_account_masked");
      const healthCertificateValidUntil = contractValue(contract, "healthCertificateValidUntil", "health_certificate_valid_until");
      const onboardingDocumentSummary = contractValue(contract, "onboardingDocumentSummary", "onboarding_document_summary");
      const commonMeta = `
        <table class="identity-table" aria-label="계약 당사자 인적사항">
          <tbody>
            <tr>
              <th class="section-head" rowspan="3">사용자</th>
              <th>상호</th><td>${previewText(employerName, "열정국밥")}</td>
              <th>대표자</th><td>${previewText(contractValue(contract, "employerRepresentative", "employer_representative"))}</td>
            </tr>
            <tr>
              <th>사업자등록번호</th><td>${previewText(contractValue(contract, "employerRegistrationNo", "employer_registration_no"))}</td>
              <th>연락처</th><td>${previewText(contractValue(contract, "employerPhone", "employer_phone"))}</td>
            </tr>
            <tr>
              <th>주소</th><td colspan="3">${previewText(contractValue(contract, "employerAddress", "employer_address"))}</td>
            </tr>
            <tr>
              <th class="section-head" rowspan="4">${partyLabel}</th>
              <th>성명</th><td>${previewText(employeeName)}</td>
              <th>생년월일·국적</th><td>${previewText(employeeBirthDate)} / ${previewText(employeeNationality)}</td>
            </tr>
            <tr>
              <th>주소</th><td colspan="3">${previewText(contractValue(contract, "employeeAddress", "employee_address"))}</td>
            </tr>
            <tr>
              <th>연락처</th><td>${previewText(employeePhone)}</td>
              <th>이메일</th><td>${previewText(contractValue(contract, "employeeEmail", "employee_email"))}</td>
            </tr>
            <tr>
              <th>${isFreelancerContract ? "정산계좌" : "급여계좌"}</th><td colspan="3">${previewText([bankName, bankAccountMasked, bankAccountHolder ? `예금주 ${bankAccountHolder}` : ""].filter(Boolean).join(" / "), "미등록")}</td>
            </tr>
            <tr>
              <th class="section-head" rowspan="2">서류</th>
              <th class="wide-label">입사서류 확인</th><td colspan="3">${previewText(onboardingDocumentSummary, "등록된 입사서류 없음")}</td>
            </tr>
            <tr>
              <th class="wide-label">보건증 유효기한</th><td colspan="3">${previewText(healthCertificateValidUntil, "미등록")}</td>
            </tr>
          </tbody>
        </table>
      `;
      const wageLine = `${wageTypeLabels[wageType] || wageType} ${formatMoney(wage)} / 지급일: ${payDate || "미입력"} / 지급방법: ${payMethod}`;
      let clauses = [];
      if (contractType === "freelancer") {
        clauses = [
          contractClause("계약 목적", `${employerName || "위탁자"}는 ${employeeName || "수급인"}에게 독립 용역을 의뢰하고 수급인은 계약 범위의 산출물을 제공한다.`),
          contractClause("용역 기간 및 장소", `${startDate || "용역 시작일 미입력"}부터 ${endDate || "종료일 별도 합의"}까지 수행한다. 수행 장소·일정은 산출물 납품과 검수에 필요한 범위에서 협의하며 고정 출퇴근, 매장 상시 근무표, 교대조 편입으로 운영하지 않는다.`),
          contractClause("업무범위, 산출물, 검수", freelancerScope || `${jobDescription || "업무 범위 미입력"} / 산출물, 납품 방식, 검수 기준, 재작업 범위를 확정한다.`),
          contractClause("용역비 및 정산", `${wageLine}. ${freelancerSettlementTerms || "검수 완료 후 합의 지급일에 지급하고 3.3% 사업소득 원천징수 및 비용 부담 주체를 확인한다."}`),
          contractClause("독립성 및 근로자성 점검", "수급인은 독립 사업자로서 업무수행방법을 스스로 정한다. 고정 근무, 상시 지휘감독, 대체인력 제한, 매장 상시업무 편입 등 근로자성 징표가 생기면 계약 명칭과 무관하게 근로계약 전환을 검토한다."),
          contractClause("안전보건 및 협조", "위탁자는 용역 수행에 필요한 자료와 안전·보건상 유의사항을 안내하고, 수급인은 관련 법령과 사업장 안전·위생 기준을 준수한다."),
          contractClause("비밀유지 및 자료보호", confidentialityTerms || "매출자료, 고객정보, 레시피, 정산자료, 계정정보를 외부에 공개하지 않는다."),
          contractClause("계약 해지 및 기성 정산", terminationTerms || "중대한 계약 위반, 검수 불합격 반복, 보안 위반 시 서면 통지 후 해지할 수 있으며 검수 완료 산출물은 별도 정산한다.")
        ];
      } else if (contractType === "confidentiality") {
        clauses = [
          contractClause("서약 목적", `${employeeName || "서약자"}는 근무 또는 용역 수행 중 알게 된 영업비밀과 개인정보를 보호한다.`),
          contractClause("비밀정보 범위", "고객정보, 직원정보, 매출·정산자료, 원가·매입자료, 레시피, 영업 매뉴얼, 계정정보, 시스템 접근정보를 포함한다."),
          contractClause("금지 행위", "무단 열람, 반출, 촬영, 복사, 제3자 제공, 개인 저장매체 보관, 퇴사 후 사용을 금지한다."),
          contractClause("반환 및 삭제", "퇴사·계약 종료 또는 사용자 요청 시 보유 자료를 즉시 반환하고 개인 기기·계정의 사본을 삭제한다."),
          contractClause("책임", confidentialityTerms || "위반 시 회사 손해, 고객 피해, 법령 위반에 대한 책임을 부담할 수 있다.")
        ];
      } else {
        const partTimeNote = contractType === "part_time"
          ? ` 근로일별 근로시간: ${dailyWorkSchedule || "미입력"}.`
          : "";
        const managerNote = contractType === "manager"
          ? "매니저는 포스 마감, 현금·정산, 위생·근태 점검, 직원 교육, 배달앱 운영계정 관리 책임을 포함한다."
          : "";
        clauses = [
          contractClause("근로계약 기간", `${startDate || "입사일 미입력"}부터 ${endDate || "기간의 정함 없음"}까지로 한다. ${contractType === "regular" ? "정규직은 기간의 정함이 없는 계약으로 관리한다." : ""}`),
          contractClause("근무장소 및 업무", `${workplace || "근무장소 미입력"}에서 ${jobDescription || "업무내용 미입력"} 업무를 수행한다. ${managerNote}`),
          contractClause("소정근로시간 및 휴게", `${workTime || "근무시간 미입력"} / 휴게 ${restTime || "미입력"} / 주 소정근로시간 ${weeklyHours || "미입력"}. ${partTimeNote}`),
          contractClause("근무일, 휴일, 주휴", `근무일은 ${workDays || "미입력"}, 휴일은 ${holidays || "미입력"}로 한다.${partTimeNote}`),
          contractClause("임금, 계산방법, 지급방법", `${wageLine}. 임금 구성: ${wageComposition || "기본급, 법정수당, 공제항목 미입력"}. 최저임금 및 임금명세서 교부 기준을 준수한다.`),
          contractClause("연장·야간·휴일근로", overtimeTerms || "연장·야간·휴일근로는 사전 승인 기준으로 운영하고 법정 가산수당을 급여마감 시 반영한다."),
          contractClause("휴가, 결근, 근태", leaveTerms || "연차유급휴가, 지각, 조퇴, 결근, 병가 처리는 근태기록과 법정 기준에 따라 급여내역서에 반영한다."),
          contractClause("4대보험 및 세무", insuranceTerms || `${taxLabel} 기준으로 취득 신고, 원천세, 급여명세서 교부를 처리한다.`),
          contractClause("수습 및 평가", contractValue(contract, "probationPeriod", "probation_period") || "수습기간 해당 없음. 수습이 있는 경우 기간, 평가 기준, 임금 적용 여부를 별도로 명시한다."),
          contractClause("식대, 유니폼, 보건증", mealUniformTerms || "식사 제공, 유니폼 지급·반납, 보건증 제출·갱신, 위생교육 이수 기준을 준수한다."),
          contractClause("퇴직 및 계약 해지", terminationTerms || "퇴직 시 사전 통보, 인수인계, 무단결근·중대한 위반 시 계약 해지 기준을 적용한다."),
          contractClause("개인정보, 보안, 위생", confidentialityTerms || "고객정보, 매출자료, 레시피, 배달앱·포스 계정정보, 위생수칙을 준수하고 무단 반출·공유를 금지한다.")
        ];
      }
      if (foreignWorker) {
        clauses.push(contractClause("외국인 채용 확인", `체류자격 ${contractValue(contract, "visaStatus", "visa_status") || "미입력"}, 외국인등록번호 ${contractValue(contract, "foreignRegistrationNoMasked", "foreign_registration_no_masked") || "마스킹본 미입력"}. 채용 전 취업활동 가능 범위, 체류기간, 고용허가/신고 필요 여부를 확인하고 입사서류 검수에 기록한다.`));
      }
      const minorGuardianName = contractValue(contract, "minorGuardianName", "minor_guardian_name");
      if (minorGuardianName) {
        clauses.push(contractClause("연소근로자 보호", `친권자/후견인 ${minorGuardianName}, 연락처 ${contractValue(contract, "minorGuardianPhone", "minor_guardian_phone") || "미입력"}. 연령증명서와 친권자/후견인 동의서를 사업장에 비치한다.`));
      }
      clauses.push(contractClause(
        isFreelancerContract ? "계약서 제공 및 전자서명" : isConfidentialityContract ? "서약서 제공 및 전자서명" : "계약서 교부 및 전자서명",
        isFreelancerContract
          ? "본 용역계약서는 전자서명 요청 링크로 수급인에게 제공하며, 서명 완료본과 정산·검수 기록을 문서보관함에 보관한다."
          : isConfidentialityContract
            ? "본 서약서는 전자서명 요청 링크로 서약자에게 제공하며, 서명 완료본을 문서보관함에 보관한다."
            : "사업주는 근로계약 체결 시 본 계약서를 근로자에게 교부하며, 전자서명 완료본과 급여/입사서류 기록을 직원관리 화면에 보관한다."
      ));
      const contractBusinessId = contractValue(contract, "businessId", "business_id") || businessForBranchName(contractValue(contract, "branch")).id || "";
      const stampUrl = ["biz-junghwa", "biz-sungshin", "biz-mia"].includes(contractBusinessId) ? `assets/stamps/${contractBusinessId}.png` : "";
      const stampAlt = `${employerName || "사용자"} 전자문서용 확인도장`;
      const legalBasisText = isFreelancerContract
        ? "고용노동부 노무제공자 공통 표준계약서 및 활용가이드(2023-12-26 게시)를 기준으로 업무범위, 산출물, 검수, 용역비, 비용 부담, 비밀유지, 해지 기준을 명확히 하는 내부 양식입니다. 3.3% 원천징수는 세무 처리 기준일 뿐 근로자성 판단을 대체하지 않습니다."
        : isConfidentialityContract
          ? "보안/개인정보 서약서는 근로계약 또는 용역계약과 별도로 체결하는 부속 문서입니다. 개인정보보호와 영업비밀 보호 범위, 반환·삭제 의무를 분리해 관리합니다."
          : "고용노동부 2025년 개정 표준근로계약서 기준 참고. 고용노동부 표준근로계약서(서식 모음) 필수 기재축과 2026년 적용 최저임금 고시 시간급 10,320원 기준을 반영한 내부 작성본입니다. 최종 체결 전 실제 근무표, 상시근로자 수, 적용 법령을 확인하십시오.";
      const legalChecklist = isFreelancerContract
        ? ["업무범위·산출물·검수 기준", "용역비·정산일·3.3% 원천징수", "비용 부담과 자료 소유·반환", "지휘·감독 및 고정근무 배제", "근로자성 발견 시 근로계약 전환", "A4 계약서와 검수·정산 기록 보관"]
        : isConfidentialityContract
          ? ["비밀정보와 개인정보 범위", "접근·복사·반출 금지", "계약 종료 후 반환·삭제", "위반 시 책임 범위"]
          : ["임금과 임금 구성항목·계산방법·지급방법", "소정근로시간·근무일·휴게시간", "휴일·주휴·연차유급휴가", "근무장소와 업무내용", "계약기간 및 계약서 교부", "A4 서면·전자문서 보관"];
      const legalChecklistHtml = `<div class="legal-checklist"><b>${isFreelancerContract ? "용역계약 필수 확인" : isConfidentialityContract ? "서약서 필수 확인" : "근로기준법 제17조 필수 확인"}</b><ul>${legalChecklist.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>`;
      const classificationWarningHtml = isFreelancerContract
        ? `<div class="classification-warning"><b>프리랜서 오분류 방지</b><br>매장 상시업무에 투입되고 출퇴근·근무시간·업무수행 방식에 대한 직접 지휘·감독을 받으면 계약 명칭이나 3.3% 원천징수와 무관하게 근로계약으로 보아야 할 수 있습니다. 이 경우 4대보험, 최저임금, 휴게, 임금명세서, 해고예고 등 근로관계 기준을 재검토하십시오.</div>`
        : "";
      const salaryBreakdown = contractType === "regular" && (baseSalary || nonTaxMealAllowance || taxableAllowance)
        ? `<div class="salary-breakdown"><span>과세 기본급<br><b>${formatMoney(baseSalary)}</b></span><span>비과세 식대<br><b>${formatMoney(nonTaxMealAllowance)}</b><br><small>${mealProvision === "cash_no_meal" ? "식사 미제공 조건" : "미적용"}</small></span><span>기타 과세수당<br><b>${formatMoney(taxableAllowance)}</b></span></div>`
        : "";
      return `
        <h3>${previewText(title, "계약서")}</h3>
        <p class="preview-meta">계약 체결일 ${previewText(contractDate, "미입력")} · ${isFreelancerContract ? "수행 지점" : "근무지점"} ${previewText(contractValue(contract, "branch"))} · A4 210mm x 297mm · 최신양식 v2026.07.23 · ${isFreelancerContract ? "고용노동부 노무제공자 공통 표준계약서 기준 참고" : isConfidentialityContract ? "부속 서약서" : "고용노동부 표준근로계약서 기준 참고"}</p>
        <div class="legal-basis">${escapeHtml(legalBasisText)}</div>
        ${commonMeta}
        ${legalChecklistHtml}
        ${classificationWarningHtml}
        ${salaryBreakdown}
        <h4>${contractDetailHeading}</h4>
        <ol>${clauses.join("")}</ol>
        ${contractValue(contract, "terms", "terms") ? `<h4>추가 특약</h4><p>${previewText(contractValue(contract, "terms", "terms"))}</p>` : ""}
        <div class="signature-grid">
          <div class="signature-box employer-signature"><b>사용자 서명</b><br>${previewText(employerName, "열정국밥")}<br>대표 ${previewText(contractValue(contract, "employerRepresentative", "employer_representative"))} (서명 또는 인)${stampUrl ? `<img class="contract-stamp" src="${escapeHtml(stampUrl)}" alt="${escapeHtml(stampAlt)}">` : ""}</div>
          <div class="signature-box"><b>${signatureLabel}</b><br>${previewText(signerName)}<br>${signatureDataUri ? `<img class="signed-signature-image" src="${escapeHtml(signatureDataUri)}" alt="${escapeHtml(signatureLabel)} 자필서명">서명일시: ${previewText(String(signedAt).replace("T", " ").slice(0, 19))}` : "서명: __________"}</div>
        </div>
      `;
    }


    const CONTRACT_STATUS = { draft: ["작성중", "info"], requested: ["서명요청", "warn"], signed: ["서명완료", "good"], archived: ["보관", "info"], cancelled: ["취소", "bad"] };
    const EMPLOYMENT_TYPES = ["part_time", "regular", "manager"];

    /** 계약 유형·신고 구분에 맞는 표준 조항 기본값(기존 buildContractDefaults 와 같은 병합 순서). */
    function defaultsFor(contractType, taxType) {
      return {
        ...standardContractDefaults.common,
        ...(standardContractDefaults[contractType] || {}),
        ...(standardContractDefaults[taxType] || {})
      };
    }

    /** 계약 유형이 정하는 신고 구분·임금 방식(기존 syncContractClassification). */
    function classificationFor(contractType) {
      if (contractType === "freelancer") return { employmentTaxType: "freelancer_33", wageType: "case_fee" };
      if (EMPLOYMENT_TYPES.includes(contractType)) return { employmentTaxType: "four_insurance", wageType: contractType === "part_time" ? "hourly" : "monthly" };
      return {};
    }

    /** 검증 결과를 예외 대신 값으로 돌려준다 — 화면이 빠진 칸을 표시할 수 있게. */
    function checkDraft(draft) {
      try {
        validateContractDraft(draft);
        return { ok: true, missing: [], message: "" };
      } catch (error) {
        const message = String(error?.message || error);
        const marker = "필수 입력값을 확인하십시오: ";
        const missing = message.startsWith(marker) ? message.slice(marker.length).split(", ").filter(Boolean) : [];
        return { ok: false, missing, message };
      }
    }

    const api = {
      contractTypeLabels, employmentTaxTypeLabels, wageTypeLabels, standardContractDefaults,
      MINIMUM_HOURLY_WAGE_2026, CONTRACT_STATUS, EMPLOYMENT_TYPES,
      defaultsFor, classificationFor, validateContractDraft, checkDraft, contractPreviewHtml,
      escapeHtml, formatMoney, today
    };
    root.ObysContractCore = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
