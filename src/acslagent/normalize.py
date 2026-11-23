"""FB4C reply processing, preserving statement order and upstream quirks.

Ported from LLM4Veri/src/llmveri.py (Apache-2.0). The frozen reference
and its SHA-256 hashes are under tests/reference_fb4c. In particular, this
module deliberately preserves set iteration, in-place list removals and the
Iteration==3 stale-variable behavior. Do not simplify without differential tests.
"""
from itertools import combinations
import re

def is_inequality(str_input):
    if "loop invariant " in str_input and \
        "||" not in str_input and \
        "&&" not in str_input and \
        '==>' not in str_input and \
        '==' not in str_input and \
        str_input.count(' > ') + str_input.count(' >= ') + str_input.count(' < ') + str_input.count(' <= ') >= 1:
        return True
    else:
        return False


def inequality_segmentation(input_str):
    # print("@@@@@@@@@@@@@@@@@@@@@@", input_str)
    if input_str.endswith(";"):
        input_str = input_str[:-1]
    # 初始化一个空列表
    inequality = []
    inequality_list = []
    # 用空格分割输入的字符串
    tokens = input_str.split()
    # 遍历每个token
    for i in range(0, len(tokens)):
        if tokens[i] == '<' or tokens[i] == '<=' or tokens[i] == '>' or tokens[i] == '>=':
            # 获取token的左边和右边的变量或表达式
            this_s = tokens[i]
            left = ""
            right = ""
            # from i to 0 find the first variable or expression
            for j in range(i-1, -1, -1):
                if tokens[j] == '<' or tokens[j] == '<=' or tokens[j] == '>' or tokens[j] == '>=':
                    left = ' '.join(tokens[j+1:i])
                    break
                elif j == 0:
                    left = ' '.join(tokens[j:i])
                else:
                    continue
            # from i to the end find the first variable or expression
            for j in range(i+1, len(tokens)):
                if tokens[j] == '<' or tokens[j] == '<=' or tokens[j] == '>' or tokens[j] == '>=':
                    right = ' '.join(tokens[i+1:j])
                    break
                elif j == len(tokens)-1:
                    right = ' '.join(tokens[i+1:j+1])
                else:
                    continue
            if left != "" and right != "":
                inequality_list.append([left, this_s, right])
        else:
            continue
    
    # 规范成小于号方向
    for each in inequality_list:
        if each[1] == '>':
            inequality.append(each[2] + " < " + each[0])
        elif each[1] == '>=':
            inequality.append(each[2] + " <= " + each[0])
        else:
            inequality.append(each[0] + " " + each[1] + " " + each[2])
    
    # print("@@@@@@@@@@@@@@@@@@@@@@", inequality, "\n")
    # 返回不等式列表
    return inequality

def clean_reply(reply):
    full_reply_content_list = [reply]
    i = 0
    full_reply_content_list[i] = full_reply_content_list[i].replace("```c;", "")
    full_reply_content_list[i] = full_reply_content_list[i].replace("```c", "")
    full_reply_content_list[i] = full_reply_content_list[i].replace("```;", "")
    full_reply_content_list[i] = full_reply_content_list[i].replace("```", "")
    full_reply_content_list[i] = full_reply_content_list[i].replace("/*@", "")
    full_reply_content_list[i] = full_reply_content_list[i].replace("*/", "")
    full_reply_content_list[i] = full_reply_content_list[i].replace("/* @ >>> INFILL <<< */", "")
    return full_reply_content_list[0]


def build_clauses(reply, existing):
    return pool_replies([clean_reply(reply)], existing)


def pool_replies(replies, existing_spec_list, *, with_context=False):
    generated_acsl_spec_set = set()
    for each_reply in replies:
        for each_spec in each_reply.split("\n"):
            # add ;
            if each_spec.endswith(";"):
                each_spec = each_spec.strip()
            else:
                each_spec = each_spec.strip() + ";"

            if each_spec.startswith("//"):
                continue
            elif "/* @ >>> INFILL <<< */" in each_spec:
                continue
            elif each_spec.strip() == "*/;" or each_spec.strip() == "/*@" or each_spec.strip() == "};":
                continue
            elif each_spec.startswith("@ "):
                each_spec = each_spec.replace("@ ", "")

            # split loop assigns
            if "loop assigns " in each_spec:
                if "unknown1" in each_spec or "unknown2" in each_spec or "unknown3" in each_spec or "unknown4" in each_spec:
                    continue
                each_spec = each_spec.replace(", ", ",")
                if "," in each_spec:
                    strip_multi_var = each_spec.replace("loop assigns ", "").replace(";", "").strip()
                    for each_var in strip_multi_var.split(","):
                        tmp_spec = "loop assigns " + each_var + ";"
                        if tmp_spec not in existing_spec_list:
                            generated_acsl_spec_set.add(tmp_spec)
                else:
                    generated_acsl_spec_set.add(each_spec)
            else:        
                if "UNINITIALIZED" in each_spec:
                    continue
                elif "unknown1()" in each_spec or "unknown2()" in each_spec or "unknown3()" in each_spec or "unknown4()" in each_spec:
                    continue
                elif "loop variant " in each_spec:
                    each_spec = each_spec.replace("loop variant ", "loop invariant ")
                elif "inner loop " in each_spec:
                    each_spec = each_spec.replace("inner loop ", "loop ")

                # format inequality
                if '==>' not in each_spec and ' ==' not in each_spec and '== ' not in each_spec and '==' in each_spec:
                    each_spec = each_spec.replace('==', ' == ')
                elif ' <=' not in each_spec and '<= ' not in each_spec and '<=' in each_spec:
                    each_spec = each_spec.replace('<=', ' <= ')
                elif ' <' not in each_spec and '< ' not in each_spec and '<' in each_spec:
                    each_spec = each_spec.replace('<', ' < ')
                elif ' >=' not in each_spec and '>=' not in each_spec and '>=' in each_spec:
                    each_spec = each_spec.replace('>=', ' >= ')
                elif ' >' not in each_spec and '> ' not in each_spec and '>' in each_spec:
                    each_spec = each_spec.replace('>', ' > ')

                # replace space
                if '  ' in each_spec:
                    each_spec = each_spec.replace('  ', ' ')

                # replace space
                if ' ;' in each_spec:
                    each_spec = each_spec.replace(' ;', ';')

                # remove '
                if "' " in each_spec:
                    each_spec = each_spec.replace("' ", " ")

                # change order of inequality
                if is_inequality(each_spec):
                    ine_spec = each_spec.replace("loop invariant ", "").strip()
                    inequality_list = inequality_segmentation(ine_spec)
                    for each_ine in inequality_list:
                        loop_inv_spec = "loop invariant " + each_ine + ';'
                        if loop_inv_spec not in existing_spec_list:
                            generated_acsl_spec_set.add(loop_inv_spec)
                    continue

                # add to set
                if each_spec not in existing_spec_list and each_spec.strip() != ";":
                    generated_acsl_spec_set.add(each_spec)
    result = list(generated_acsl_spec_set)
    return (result, each_spec) if with_context else result


def mutation_enabled(iteration, requested):
    return True if iteration in (3, 4) else False if iteration >= 5 else requested


def mutate(clauses, iteration, existing, requested=True, *, last_spec=None):
    if last_spec is not None:
        each_spec = last_spec
    generated_acsl_spec_list = list(clauses)
    SAVE_PICKLE = {"Iteration": iteration, "CurTaskID": 1, "1": existing}
    enable_mutation = mutation_enabled(iteration, requested)
    selected_inequality_list = []
    partial_order_reinforcement_list = []
    inequality_mutation_list = []
    if enable_mutation:
        for each_spec in generated_acsl_spec_list:
            if is_inequality(each_spec):
                print(each_spec)
                selected_inequality_list.append(each_spec.replace(";", "").replace("loop invariant ", "").strip())
                if " > " in each_spec and " >= " not in each_spec and " < " not in each_spec and " <= " not in each_spec:
                    partial_order_reinforcement_list.append(each_spec.replace(" > ", " >= ").strip())
                elif " >= " in each_spec and " > " not in each_spec and " < " not in each_spec and " <= " not in each_spec:
                    partial_order_reinforcement_list.append(each_spec.replace(" >= ", " > ").strip())
                elif " < " in each_spec and " <= " not in each_spec and " > " not in each_spec and " >= " not in each_spec:
                    partial_order_reinforcement_list.append(each_spec.replace(" < ", " <= ").strip())
                elif " <= " in each_spec and " < " not in each_spec and " > " not in each_spec and " >= " not in each_spec:
                    partial_order_reinforcement_list.append(each_spec.replace(" <= ", " < ").strip())

        if SAVE_PICKLE['Iteration'] == 3:
            for each_item in SAVE_PICKLE[str(SAVE_PICKLE['CurTaskID'])]:
                if is_inequality(each_spec):
                    if " > " in each_spec and " >= " not in each_spec and " < " not in each_spec and " <= " not in each_spec:
                        partial_order_reinforcement_list.append(each_spec.replace(" > ", " >= ").strip())
                    elif " >= " in each_spec and " > " not in each_spec and " < " not in each_spec and " <= " not in each_spec:
                        partial_order_reinforcement_list.append(each_spec.replace(" >= ", " > ").strip())
                    elif " < " in each_spec and " <= " not in each_spec and " > " not in each_spec and " >= " not in each_spec:
                        partial_order_reinforcement_list.append(each_spec.replace(" < ", " <= ").strip())
                    elif " <= " in each_spec and " < " not in each_spec and " > " not in each_spec and " >= " not in each_spec:
                        partial_order_reinforcement_list.append(each_spec.replace(" <= ", " < ").strip())

        if selected_inequality_list != []:
            # get combinations
            inequality_mutation_list = list(combinations(selected_inequality_list, 2))
            # get union
            for each_ineq in inequality_mutation_list:
                generated_acsl_spec_list.append("loop invariant " + each_ineq[0] + " || " + each_ineq[1] + ";")

        if partial_order_reinforcement_list != []:
            for each_ineq in partial_order_reinforcement_list:
                generated_acsl_spec_list.append(each_ineq)

    del selected_inequality_list, inequality_mutation_list
    # =============mutation ends=============

    # remove repetitive specifications
    for each_item in SAVE_PICKLE[str(SAVE_PICKLE['CurTaskID'])]:
        if each_item in generated_acsl_spec_list:
            generated_acsl_spec_list.remove(each_item)
    return generated_acsl_spec_list


def benchmark_adjustments(clauses, source_path, mode, is_outer):
    generated_acsl_spec_list = list(clauses)
    GPT_File = source_path
    GPT_Task = {"loopinv_gen": 1, "contract_gen": 2, "loopinv_infill": 3, "contract_infill": 4}[mode]
    is_outter_loop = is_outer
    if "bubble" in GPT_File or "bufs_differ" in GPT_File or "parse_null" in GPT_File or "parse_algoid_params" in GPT_File:
        if GPT_Task == 1 or GPT_Task == 3:
            print(generated_acsl_spec_list)
            for each_spec in generated_acsl_spec_list:
                if is_outter_loop == False:
                    if "loop assigns a[0..n-1]" in each_spec or "loop assigns a[0..n]" in each_spec or "loop assigns i" in each_spec or "loop assigns a[0..i-1]" in each_spec:
                        generated_acsl_spec_list.remove(each_spec)
                elif is_outter_loop == True:
                    if "loop assigns a[0..i];" in each_spec or "loop assigns a[0..i-1];" in each_spec :
                        generated_acsl_spec_list.remove(each_spec)
                    elif ("bufs_differ" in GPT_File or "parse_null" in GPT_File or "parse_algoid_params" in GPT_File) and ("loop assigns ret;" in each_spec or "loop assigns b1[0..n-1];" in each_spec or "loop assigns b2[0..n-1];" in each_spec or "loop assigns n;" in each_spec or "loop assigns b1;" in each_spec or "loop assigns b2;" in each_spec):
                        generated_acsl_spec_list.remove(each_spec)
            if is_outter_loop == False:
                if "loop assigns a[0..i];" not in generated_acsl_spec_list:
                    generated_acsl_spec_list.append("loop assigns a[0..i];")
                if "loop invariant 0 <= j;" not in generated_acsl_spec_list:
                    generated_acsl_spec_list.append("loop invariant 0 <= j;")
                if "loop invariant j <= i;" not in generated_acsl_spec_list:
                    generated_acsl_spec_list.append("loop invariant j <= i;")
            elif is_outter_loop == True:
                if "loop assigns a[0..n-1];" not in generated_acsl_spec_list:
                    generated_acsl_spec_list.append("loop assigns a[0..n-1];")
                if "loop assigns i;" not in generated_acsl_spec_list:
                    generated_acsl_spec_list.append("loop assigns i;")
                if "loop assigns j;" not in generated_acsl_spec_list:
                    generated_acsl_spec_list.append("loop assigns j;")
                if "loop assigns temp;" not in generated_acsl_spec_list:
                    generated_acsl_spec_list.append("loop assigns temp;")
                if "loop invariant i < n;" not in generated_acsl_spec_list:
                    generated_acsl_spec_list.append("loop invariant i < n;")
                if "loop invariant \\forall integer k; i <= k < n-1 ==> a[k] <= a[k+1];" not in generated_acsl_spec_list:
                    generated_acsl_spec_list.append("loop invariant \\forall integer k; i <= k < n-1 ==> a[k] <= a[k+1];")
        elif GPT_Task == 2 or GPT_Task == 4:
            if "bufs_differ" in GPT_File or "parse_null" in GPT_File or "parse_algoid_params" in GPT_File:
                for i in range(0, len(generated_acsl_spec_list)):
                    if " ==> " in generated_acsl_spec_list[i]:
                        generated_acsl_spec_list[i] = generated_acsl_spec_list[i].replace(" ==> \\exists", " <==> \\exists").replace(" ==> \\forall", " <==> \\forall")
            if "parse_null" in GPT_File:
                if "ensures \\result == 0 <==> \\forall integer i; 0 <= i < n ==> b1[i] == b2[i];" not in generated_acsl_spec_list:
                    generated_acsl_spec_list.append("ensures \\result == 0 <==> \\forall integer i; 0 <= i < n ==> b1[i] == b2[i];")
                if "ensures \\result == 1 <==> \\exists integer i; 0 <= i < n && b1[i] != b2[i];" not in generated_acsl_spec_list:
                    generated_acsl_spec_list.append("ensures \\result == 1 <==> \\exists integer i; 0 <= i < n && b1[i] != b2[i];")
    return generated_acsl_spec_list


def has_behavior(clauses):
    return any("behavior " in c or "assumes " in c for c in clauses)
