export const routes=['班次清单','样本质检','关联分析','改善建议','月度索赔'];export const page=()=>Math.max(0,Math.min(routes.length-1,Number(location.hash.slice(1)||0)));
