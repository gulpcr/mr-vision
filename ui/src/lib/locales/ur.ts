import type { Strings } from "./en";

// Urdu catalog — must mirror en.ts's key shape exactly.
export const ur: Strings = {
  nav: {
    dashboard: "ڈیش بورڈ",
    worklist: "ورک لسٹ",
    remoteReading: "ریموٹ ریڈنگ",
    reports: "رپورٹس",
    uploadDicom: "ڈائیکوم اپ لوڈ کریں",
  },
  worklist: {
    title: "ورک لسٹ",
    searchPlaceholder: "مریض، MRN، تفصیل، ایکسیشن تلاش کریں…  (/)",
    noStudiesYet: "ابھی تک کوئی اسٹڈی نہیں",
    noStudiesMatch: "آپ کی فلٹرز سے کوئی اسٹڈی مماثل نہیں",
    noStudiesYetDescription: "شروع کرنے کے لیے ڈائیکوم اسٹڈیز اپ لوڈ کریں",
    noStudiesMatchDescription: "اپنی فلٹرز کو ایڈجسٹ یا صاف کرنے کی کوشش کریں",
    clearFilters: "فلٹرز صاف کریں",
    uploadCta: "ڈائیکوم اپ لوڈ کریں",
    columnPriority: "ترجیح",
    columnPatient: "مریض",
    columnStudy: "اسٹڈی",
    columnDate: "تاریخ",
    columnBodyPart: "جسمانی حصہ",
    columnAiStatus: "AI کی صورتحال",
  },
  statusBadge: {
    unclaimed: "غیر تفویض شدہ",
    reading: "زیر مطالعہ",
    reported: "رپورٹ شدہ",
    signedOff: "دستخط شدہ",
    preliminary: "ابتدائی",
  },
  reportShell: {
    signOffReport: "رپورٹ پر دستخط کریں",
    signOffConsequence:
      "دستخط کرنے سے اس اسٹڈی کی ریڈنگ کی صورتحال حتمی طور پر مکمل ہو جائے گی اور یہ آپ کے اکاؤنٹ کے خلاف درج کی جائے گی۔ اس اسکرین سے اس عمل کو واپس نہیں کیا جا سکتا۔",
    radiologistOnly: "صرف ایک ریڈیالوجسٹ یا ایڈمن ہی اس رپورٹ پر دستخط کر سکتا ہے۔",
  },
  aiProvenance: {
    disclaimer:
      "یہ ایک AI کی تیار کردہ تجزیہ ہے جس کا مقصد طبی فیصلہ سازی میں مدد کرنا ہے۔ یہ ماہر طبی رائے کا متبادل نہیں ہے۔ تمام نتائج کو طبی استعمال سے پہلے کسی مستند ریڈیالوجسٹ یا معالج کی جانب سے جانچا اور تصدیق کیا جانا ضروری ہے۔",
  },
};
